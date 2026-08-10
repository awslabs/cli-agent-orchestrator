"""Truthful live legacy identity audit and explicit one-candidate migration
(cond-0377D).

Covers the reviewed contract end to end over the same fakes the
cond-0377C repair suite uses:

* A. read-only live audit: every classification (eligible, dead, ambiguous,
  missing-occurrence, unreadable, corrupt, unsupported, known-id, retired,
  conflicting-owner, missing-agent, kimi-no-session) with zero mutation and
  zero provider/pane bytes.
* B. exact evidence binding: the candidate digest reproduces from current
  facts; a digest that changes between audit and migration refuses before
  repair.
* C. explicit one-candidate migration: intent-first persistence, a durable
  repair-attempt-started marker persisted BEFORE any /status action, exact
  retry query-adopts the same migration AND repair operations (no second
  status interaction), response loss without adoptable evidence is typed
  ambiguous/unresolved and never resends, a changed request under the same
  id conflicts before repair.
* D. known native ids are never overwritten; the legacy callback-target
  occurrence stays the physical occurrence while the roster generation
  remains null.
* E. batch/operator iteration stops truthfully; rollback disables new work
  while retaining and query-adopting prior rows.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Optional

import pytest

from cli_agent_orchestrator import constants
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.tmux import PaneControlIdentity, TmuxClient
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import execution_mode as em
from cli_agent_orchestrator.services import legacy_identity_migration as lim
from cli_agent_orchestrator.services import managed_launch_v2 as v2
from cli_agent_orchestrator.services import native_attachment
from cli_agent_orchestrator.services import native_pane_input as npi
from cli_agent_orchestrator.services import native_status_repair as nsr
from cli_agent_orchestrator.services import pane_input_arbiter as pia
from cli_agent_orchestrator.services import stable_agent_roster as roster

TERMINAL_ID = "a1b2c3d4"
GENERATION = "00000000-0000-4000-8000-000000000001"
CALLBACK_TARGET = "00000000-0000-4000-8000-0000000000aa"
PANE_ID = "%7"
WINDOW_ID = "@7"
TMUX_SESSION_ID = "$1"
SERVER_SOCKET = "/private/tmp/cao-native.sock"
PANE_PID = 4242
START_MARKER = "Thu Jul 24 10:00:00 2026"
SESSION_NAME = "cao-campaign"

CLAUDE_VERSION = "2.1.226"
SESSION_ID = "4f5f46c7-b660-4f6f-a144-d2c6dceccf95"

_AGENT_ID_1 = "11111111-1111-4111-8111-111111111111"


def _uuid() -> str:
    return str(uuid.uuid4())


def claude_panel_rows(session_id: str = SESSION_ID) -> list[str]:
    return [
        "Settings  Status   Config   Usage   Stats",
        "",
        "Version:          2.1.226",
        "Session name:     /rename to add a name",
        f"Session ID:       {session_id}",
        "Session kind:     interactive",
        "cwd:              /Users/x/repo",
        "Login method:     <redacted>",
        "Organization:     <redacted>",
        "Email:            <redacted>",
        "",
        "Model:            opus[1m] (claude-opus-5[1m])",
        "MCP servers:      <variable provider state>",
        "Setting sources:  User settings",
        "",
        "Esc to cancel",
    ]


def claude_composer_rows() -> list[str]:
    return [
        "-------------------------------------------------------------------------------",
        "> ",
        "-------------------------------------------------------------------------------",
        "<quota/model/cwd status line>",
    ]


# ---------------------------------------------------------------------------
# Harness (same seams as the cond-0377C repair suite)
# ---------------------------------------------------------------------------


class _MigrationHarness:
    def __init__(self) -> None:
        self.typed: list[dict[str, Any]] = []
        self.screens: list[list[str]] = []
        self.styled_screens: list[list[str]] = []
        self.turn_states: list[TerminalStatus] = [TerminalStatus.IDLE]
        self.pane_identity: Optional[PaneControlIdentity] = PaneControlIdentity(
            pane_id=PANE_ID,
            window_id=WINDOW_ID,
            session_id=TMUX_SESSION_ID,
            pane_pid=PANE_PID,
            session_name=SESSION_NAME,
            window_name=f"w-{TERMINAL_ID}",
            bracketed_paste_proven=False,
            dead=False,
            server_socket_path=SERVER_SOCKET,
        )
        self.pane_identity_error: Optional[Exception] = None
        self.server_identity: Optional[str] = SERVER_SOCKET
        self.live_start_marker: Optional[str] = START_MARKER
        self.capture_errors: list[Exception] = []
        self.calls: list[str] = []

    def turn_state(self, pane_id: str, **_kwargs: Any) -> TerminalStatus:
        self.calls.append("turn-state")
        return self.turn_states[-1]

    def capture_screen(self, pane_id: str, **_kwargs: Any) -> list[str]:
        self.calls.append("capture")
        if self.capture_errors:
            raise self.capture_errors.pop(0)
        assert self.screens, "no scripted panel rows"
        return list(self.screens[-1])

    def capture_screen_styled(self, pane_id: str, **_kwargs: Any) -> list[str]:
        self.calls.append("capture-styled")
        assert self.styled_screens, "no scripted post-Escape rows"
        return list(self.styled_screens[-1])

    def pane_control_identity(self, *args: Any, **kwargs: Any) -> Optional[PaneControlIdentity]:
        self.calls.append("pane-identity")
        if self.pane_identity_error is not None:
            raise self.pane_identity_error
        return self.pane_identity

    def pane_server_identity(self, pane_id: str, *args: Any, **kwargs: Any) -> Optional[str]:
        self.calls.append("server-identity")
        return self.server_identity

    def start_marker(self, pid: int) -> Optional[str]:
        self.calls.append("start-marker")
        return self.live_start_marker

    def typed_literal(self, text: str) -> None:
        self.typed.append({"kind": "literal", "text": text})

    def typed_enter(self) -> None:
        self.typed.append({"kind": "enter"})

    def typed_key(self, keystroke: str) -> None:
        self.typed.append({"kind": "key", "keystroke": keystroke})


class _FakeTmuxPaneInput:
    _state: _MigrationHarness

    @classmethod
    def for_state(cls, state: _MigrationHarness) -> type["_FakeTmuxPaneInput"]:
        cls._state = state
        return cls

    def __init__(self, pane_id: str) -> None:
        self._pane_id = pane_id

    def send_literal(self, text: str) -> None:
        self._state.typed_literal(text)

    def send_enter(self) -> None:
        self._state.typed_enter()

    def send_key(self, keystroke: str) -> None:
        self._state.typed_key(keystroke)


@pytest.fixture(autouse=True)
def _sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "COMPANION_DIR", tmp_path / "companion")
    monkeypatch.setattr(constants, "CAO_HOME_DIR", tmp_path / "home")


@pytest.fixture
def harness(monkeypatch):
    state = _MigrationHarness()
    monkeypatch.setattr(npi, "TmuxPaneInput", _FakeTmuxPaneInput.for_state(state))
    monkeypatch.setattr(npi, "capture_pane_screen", state.capture_screen)
    monkeypatch.setattr(npi, "capture_pane_screen_styled", state.capture_screen_styled)
    for observer in (
        "observe_codex_turn_state",
        "observe_kimi_turn_state",
        "observe_claude_turn_state",
        "observe_muse_turn_state",
    ):
        monkeypatch.setattr(npi, observer, state.turn_state)
    monkeypatch.setattr(TmuxClient, "pane_control_identity", state.pane_control_identity)
    monkeypatch.setattr(TmuxClient, "observe_pane_server_identity", state.pane_server_identity)
    monkeypatch.setattr(lim, "_live_start_marker", state.start_marker)
    # The repair itself reads the start marker through its own module symbol.
    monkeypatch.setattr(lim.nsr, "_live_start_marker", state.start_marker)
    monkeypatch.setattr(v2, "NATIVE_PANE_READY_TIMEOUT_SECONDS", 0.4)
    monkeypatch.setattr(v2, "_NATIVE_PANE_READY_POLL_SECONDS", 0.005)
    return state


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


_DEFAULT_CALLBACK = object()


def _seed_legacy(
    provider: str,
    *,
    terminal_id: str = TERMINAL_ID,
    callback_target: Any = _DEFAULT_CALLBACK,
    lifecycle: str = "live",
    native_session_id: Optional[str] = None,
    pane_id: Optional[str] = PANE_ID,
) -> None:
    if callback_target is _DEFAULT_CALLBACK:
        # callback_target_generation is UNIQUE: derive a deterministic
        # per-terminal occurrence (the canonical one for the primary id).
        callback_target = CALLBACK_TARGET if terminal_id == TERMINAL_ID else f"ct-{terminal_id}"
    with database.SessionLocal() as db:
        db.add(
            database.TerminalModel(
                id=terminal_id,
                tmux_session=SESSION_NAME,
                tmux_window=f"w-{terminal_id}",
                provider=provider,
                generation=None,
                callback_target_generation=callback_target,
                pane_id=pane_id,
                window_id=WINDOW_ID,
                server_socket_path=SERVER_SOCKET,
                session_id=TMUX_SESSION_ID,
                pane_pid=PANE_PID,
                native_session_id=native_session_id,
                lifecycle_state=lifecycle,
            )
        )
        db.commit()


def _seed_shared_managed(
    provider: str,
    *,
    terminal_id: str = TERMINAL_ID,
    generation: str = GENERATION,
) -> None:
    """A v1 managed row in the shared terminals table (no v2 binding)."""
    with database.SessionLocal() as db:
        db.add(
            database.TerminalModel(
                id=terminal_id,
                tmux_session=SESSION_NAME,
                tmux_window=f"w-{terminal_id}",
                provider=provider,
                generation=generation,
                pane_id=PANE_ID,
                window_id=WINDOW_ID,
                server_socket_path=SERVER_SOCKET,
                session_id=TMUX_SESSION_ID,
                pane_pid=PANE_PID,
                native_session_id=None,
                lifecycle_state="live",
            )
        )
        db.commit()


def _seed_roster(
    provider: str,
    *,
    terminal_id: str = TERMINAL_ID,
    generation: Optional[str] = GENERATION,
    native_session_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    start_marker: str = START_MARKER,
) -> dict[str, Any]:
    if agent_id is None:
        # One stable agent per terminal in the tests (the roster enforces
        # one live incarnation per agent).
        agent_id = (
            _AGENT_ID_1
            if terminal_id == TERMINAL_ID
            else str(uuid.uuid5(uuid.NAMESPACE_DNS, f"cao-test-agent:{terminal_id}"))
        )
    return roster.bind_generation(
        roster.BindingContract(
            agent_id=agent_id,
            session_name=SESSION_NAME,
            role=roster.ROLE_WORKER,
            profile_family="developer",
            harness=provider,
            native_session_id=native_session_id,
            terminal_id=terminal_id,
            generation=generation,
            pane_id=PANE_ID,
            pane_pid=PANE_PID,
            process_identity={"pid": PANE_PID, "start_marker": start_marker},
            execution_mode=em.NATIVE_TUI,
        )
    )


def _seed_attachment(
    *,
    provider: str = "claude_code",
    native_session_id: str = SESSION_ID,
    terminal_id: str = TERMINAL_ID,
    generation: str = CALLBACK_TARGET,
    adoption_receipt: Optional[dict[str, Any]] = None,
) -> None:
    stamp = "2026-08-10T00:00:00Z"
    with database.SessionLocal() as db:
        db.add(
            database.NativeSessionAttachmentModel(
                provider=provider,
                native_session_id=native_session_id,
                state=native_attachment.ATTACHED,
                owner_terminal_id=terminal_id,
                owner_generation=generation,
                owner_execution_mode=em.NATIVE_TUI,
                owner_pane_id=PANE_ID,
                owner_process_identity_json=json.dumps(
                    {"pid": PANE_PID, "start_marker": START_MARKER}
                ),
                intent_json=json.dumps(
                    {
                        "schema": "cao-native-attachment-intent-v1",
                        "acquisition_method": native_attachment.ACQUISITION_STATUS_DISCOVERED,
                    }
                ),
                adoption_receipt_json=(
                    json.dumps(adoption_receipt) if adoption_receipt is not None else None
                ),
                epoch=0,
                created_at=stamp,
                updated_at=stamp,
            )
        )
        db.commit()


def _dump_all_rows() -> dict[str, Any]:
    """Snapshot every ORM table for zero-mutation assertions."""
    snapshot: dict[str, Any] = {}
    with database.SessionLocal() as db:
        for table in database.Base.metadata.sorted_tables:
            rows = db.execute(table.select()).all()
            snapshot[table.name] = sorted(
                (dict(row._mapping) for row in rows),
                key=lambda r: json.dumps(r, sort_keys=True, default=str),
            )
    return snapshot


def _candidate_for(terminal_id: str = TERMINAL_ID) -> dict[str, Any]:
    audit = lim.run_live_legacy_audit()
    for candidate in audit["candidates"]:
        if candidate["terminal_id"] == terminal_id:
            return candidate
    raise AssertionError(f"no audit candidate for {terminal_id}")


def _typed_bytes(state: _MigrationHarness) -> list[tuple[str, str]]:
    return [
        (entry["kind"], entry.get("text") or entry.get("keystroke") or "") for entry in state.typed
    ]


def _migration_rows() -> list[Any]:
    with database.SessionLocal() as db:
        return db.query(database.LegacyIdentityMigrationModel).all()


def _evidence_rows() -> list[Any]:
    with database.SessionLocal() as db:
        return db.query(database.NativeStatusRepairEvidenceModel).all()


def _legacy_row(terminal_id: str = TERMINAL_ID) -> Any:
    with database.SessionLocal() as db:
        return (
            db.query(database.TerminalModel)
            .filter(database.TerminalModel.id == terminal_id)
            .first()
        )


def _current_lineage(terminal_id: str = TERMINAL_ID) -> dict[str, Any]:
    with database.SessionLocal() as db:
        lineage_id = (
            db.query(database.StableAgentIncarnationModel)
            .filter(database.StableAgentIncarnationModel.terminal_id == terminal_id)
            .one()
            .lineage_id
        )
        row = (
            db.query(database.StableAgentLineageModel)
            .filter(database.StableAgentLineageModel.lineage_id == lineage_id)
            .one()
        )
        return {
            "native_session_id": row.native_session_id,
            "lineage_origin": row.lineage_origin,
            "acquisition_method": row.acquisition_method,
            "continuity_note": row.continuity_note,
        }


def _migration_call(**changes: Any) -> dict[str, Any]:
    audit = lim.run_live_legacy_audit()
    eligible = [c for c in audit["candidates"] if c["classification"] == lim.CANDIDATE_ELIGIBLE]
    assert eligible, "no eligible candidate to migrate"
    candidate = eligible[0]
    payload: dict[str, Any] = {
        "operation_id": _uuid(),
        "terminal_id": candidate["terminal_id"],
        "provider": candidate["provider"],
        "generation": candidate["generation"],
        "physical_occurrence": candidate["physical_occurrence"],
        "provider_version": CLAUDE_VERSION,
        "audit_occurrence_id": candidate["occurrence_id"],
        "audit_candidate_digest": candidate["evidence_digest"],
    }
    payload.update(changes)
    return lim.migrate_terminal_native_identity(**payload)


def _intent_row(
    *,
    operation_id: str,
    request_digest: str,
    candidate: dict[str, Any],
    repair_operation_id: str,
    status: str,
) -> None:
    """Insert the migration intent row directly (crash-state construction)."""
    stamp = "2026-08-10T00:00:00Z"
    with database.SessionLocal() as db:
        db.add(
            database.LegacyIdentityMigrationModel(
                migration_operation_id=operation_id,
                request_digest=request_digest,
                terminal_id=candidate["terminal_id"],
                provider=candidate["provider"],
                generation=candidate["generation"],
                physical_occurrence=candidate["physical_occurrence"],
                provider_version=CLAUDE_VERSION,
                audit_occurrence_id=candidate["occurrence_id"],
                audit_candidate_digest=candidate["evidence_digest"],
                repair_operation_id=repair_operation_id,
                status=status,
                created_at=stamp,
                updated_at=stamp,
            )
        )
        db.commit()


def _call_with_candidate(
    candidate: dict[str, Any], operation_id: str, **changes: Any
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "operation_id": operation_id,
        "terminal_id": candidate["terminal_id"],
        "provider": candidate["provider"],
        "generation": candidate["generation"],
        "physical_occurrence": candidate["physical_occurrence"],
        "provider_version": CLAUDE_VERSION,
        "audit_occurrence_id": candidate["occurrence_id"],
        "audit_candidate_digest": candidate["evidence_digest"],
    }
    payload.update(changes)
    return lim.migrate_terminal_native_identity(**payload)


# ---------------------------------------------------------------------------
# A. The read-only live audit
# ---------------------------------------------------------------------------


class TestLiveLegacyAudit:
    def test_eligible_legacy_candidate_binds_authoritative_occurrence(
        self, isolated_memory_db, harness
    ):
        _seed_legacy("claude_code")
        _seed_roster("claude_code", generation=None)

        candidate = _candidate_for()

        assert candidate["classification"] == lim.CANDIDATE_ELIGIBLE
        assert candidate["reason"] is None
        assert candidate["terminal_id"] == TERMINAL_ID
        assert candidate["vintage"] == "legacy"
        assert candidate["managed"] is False
        assert candidate["generation"] is None
        assert candidate["physical_occurrence"] == CALLBACK_TARGET
        assert candidate["provider"] == "claude_code"
        assert candidate["agent_id"] == _AGENT_ID_1
        # Role/profile come from the authoritative roster agent row, never
        # from profile or caller input.
        assert candidate["agent_role"] == roster.ROLE_WORKER
        assert candidate["agent_profile_family"] == "developer"
        assert candidate["incarnation_disposition"] == roster.INCARNATION_BOUND
        assert candidate["process_identity"] == {"pid": PANE_PID, "start_marker": START_MARKER}
        assert candidate["pane_live"] is True
        assert candidate["server_live"] is True
        assert candidate["process_live"] is True
        assert candidate["terminal_native_session_id"] is None
        assert candidate["lineage_native_session_id"] is None
        assert re.fullmatch(r"[0-9a-f]{64}", candidate["evidence_digest"])
        assert candidate["occurrence_id"]
        # The digest reproduces from the candidate's own bounded facts.
        assert lim.candidate_evidence_digest(candidate) == candidate["evidence_digest"]

    def test_eligible_managed_exact_generation_candidate(self, isolated_memory_db, harness):
        _seed_shared_managed("codex")
        _seed_roster("codex", generation=GENERATION)

        candidate = _candidate_for()

        assert candidate["classification"] == lim.CANDIDATE_ELIGIBLE
        assert candidate["managed"] is True
        assert candidate["generation"] == GENERATION
        assert candidate["physical_occurrence"] == GENERATION
        assert candidate["agent_role"] == roster.ROLE_WORKER

    def test_audit_is_strictly_read_only_for_every_classification(
        self, isolated_memory_db, harness
    ):
        _seed_legacy("claude_code")
        _seed_roster("claude_code", generation=None)
        _seed_legacy("claude_code", terminal_id="deadbeef", native_session_id=SESSION_ID)
        _seed_roster("claude_code", terminal_id="deadbeef", generation=None)
        _seed_legacy("claude_code", terminal_id="nooccur", callback_target=None)
        _seed_legacy("kiro_cli", terminal_id="kiro001")
        stamp = "2026-08-10T00:00:00Z"
        with database.SessionLocal() as db:
            db.add(
                database.StableAgentModel(
                    agent_id="22222222-2222-4222-8222-222222222222",
                    session_name=SESSION_NAME,
                    role=roster.ROLE_WORKER,
                    profile_family="developer",
                    disposition=roster.DISPOSITION_IDENTITY_MISSING,
                    resume_contract_version=roster.RESUME_CONTRACT_VERSION,
                    revision=1,
                    created_at=stamp,
                    updated_at=stamp,
                )
            )
            db.add(
                database.StableAgentIncarnationModel(
                    incarnation_id="33333333-3333-4333-8333-333333333333",
                    agent_id="22222222-2222-4222-8222-222222222222",
                    terminal_id="corrupt01",
                    generation=None,
                    pane_id=PANE_ID,
                    pane_pid=PANE_PID,
                    process_identity_json="{not json",
                    execution_mode=em.NATIVE_TUI,
                    disposition=roster.INCARNATION_BOUND,
                    created_at=stamp,
                    updated_at=stamp,
                )
            )
            db.commit()
        _seed_legacy("claude_code", terminal_id="corrupt01")
        _seed_legacy("claude_code", terminal_id="retired01")
        _seed_roster("claude_code", terminal_id="retired01", generation=None)
        roster.retire_incarnation(terminal_id="retired01", generation=None, reason="done")
        _seed_legacy("claude_code", terminal_id="owner01")
        _seed_roster("claude_code", terminal_id="owner01", generation=None)
        _seed_attachment(terminal_id="owner01", generation="00000000-0000-4000-8000-0000000000bb")
        # Orphan incarnation: no stable agent row -> no authoritative
        # role/profile provenance.
        _seed_legacy("claude_code", terminal_id="orphan01")
        with database.SessionLocal() as db:
            db.add(
                database.StableAgentIncarnationModel(
                    incarnation_id="55555555-5555-4555-8555-555555555555",
                    agent_id="00000000-0000-4000-8000-0000000000ff",
                    terminal_id="orphan01",
                    generation=None,
                    pane_id=PANE_ID,
                    pane_pid=PANE_PID,
                    process_identity_json=json.dumps(
                        {"pid": PANE_PID, "start_marker": START_MARKER}
                    ),
                    execution_mode=em.NATIVE_TUI,
                    disposition=roster.INCARNATION_BOUND,
                    created_at=stamp,
                    updated_at=stamp,
                )
            )
            db.commit()

        before = _dump_all_rows()
        audit = lim.run_live_legacy_audit()
        after = _dump_all_rows()

        assert after == before
        assert harness.typed == []
        assert not any("capture" in call or "turn-state" in call for call in harness.calls)

        by_terminal = {c["terminal_id"]: c for c in audit["candidates"]}
        assert by_terminal["deadbeef"]["classification"] == lim.REFUSAL_ALREADY_KNOWN
        assert by_terminal["nooccur"]["classification"] == lim.REFUSAL_MISSING_OCCURRENCE
        assert by_terminal["kiro001"]["classification"] == lim.REFUSAL_UNSUPPORTED_PROVIDER
        assert by_terminal["corrupt01"]["classification"] == lim.REFUSAL_CORRUPT
        assert by_terminal["retired01"]["classification"] == lim.REFUSAL_ALREADY_RETIRED
        assert by_terminal["owner01"]["classification"] == lim.REFUSAL_CONFLICTING_OWNER
        assert by_terminal["orphan01"]["classification"] == lim.REFUSAL_MISSING_AGENT
        assert by_terminal[TERMINAL_ID]["classification"] == lim.CANDIDATE_ELIGIBLE
        assert audit["eligible_count"] >= 1

    def test_audit_dead_and_unknown_liveness_are_explicit(self, isolated_memory_db, harness):
        _seed_legacy("claude_code", terminal_id="dead01")
        _seed_roster("claude_code", terminal_id="dead01", generation=None)
        _seed_legacy("claude_code", terminal_id="lost01")
        _seed_roster("claude_code", terminal_id="lost01", generation=None)
        _seed_legacy("claude_code", terminal_id="unread01", pane_id=None)
        _seed_roster("claude_code", terminal_id="unread01", generation=None)

        harness.pane_identity = PaneControlIdentity(
            pane_id=PANE_ID,
            window_id=WINDOW_ID,
            session_id=TMUX_SESSION_ID,
            pane_pid=PANE_PID,
            session_name=SESSION_NAME,
            window_name="w-dead01",
            bracketed_paste_proven=False,
            dead=True,
            server_socket_path=SERVER_SOCKET,
        )
        assert _candidate_for("dead01")["classification"] == lim.REFUSAL_DEAD

        harness.pane_identity = None
        assert _candidate_for("lost01")["classification"] == lim.REFUSAL_UNKNOWN_LIVENESS

        harness.pane_identity = PaneControlIdentity(
            pane_id=PANE_ID,
            window_id=WINDOW_ID,
            session_id=TMUX_SESSION_ID,
            pane_pid=PANE_PID,
            session_name=SESSION_NAME,
            window_name="w-unread01",
            bracketed_paste_proven=False,
            dead=False,
            server_socket_path=SERVER_SOCKET,
        )
        assert _candidate_for("unread01")["classification"] == lim.REFUSAL_UNREADABLE

    def test_audit_ambiguous_terminal_refuses_without_picking(self, isolated_memory_db, harness):
        _seed_legacy("claude_code")
        stamp = "2026-08-10T00:00:00Z"
        with database.SessionLocal() as db:
            for incarnation_id, generation in (
                ("44444444-4444-4444-8444-444444444441", None),
                (
                    "44444444-4444-4444-8444-444444444442",
                    "00000000-0000-4000-8000-000000000099",
                ),
            ):
                db.add(
                    database.StableAgentIncarnationModel(
                        incarnation_id=incarnation_id,
                        agent_id=_AGENT_ID_1,
                        terminal_id=TERMINAL_ID,
                        generation=generation,
                        pane_id=PANE_ID,
                        pane_pid=PANE_PID,
                        process_identity_json=json.dumps(
                            {"pid": PANE_PID, "start_marker": START_MARKER}
                        ),
                        execution_mode=em.NATIVE_TUI,
                        disposition=roster.INCARNATION_BOUND,
                        created_at=stamp,
                        updated_at=stamp,
                    )
                )
            db.commit()
        assert _candidate_for()["classification"] == lim.REFUSAL_AMBIGUOUS

    def test_audit_process_drift_and_unobservable_are_explicit(self, isolated_memory_db, harness):
        _seed_legacy("claude_code", terminal_id="marker01")
        _seed_roster("claude_code", terminal_id="marker01", generation=None, start_marker="old")
        assert _candidate_for("marker01")["classification"] == lim.REFUSAL_PROCESS_DRIFT
        harness.live_start_marker = None
        assert _candidate_for("marker01")["classification"] == lim.REFUSAL_PROCESS_UNOBSERVABLE

    def test_audit_terminal_not_live_is_explicit(self, isolated_memory_db, harness):
        _seed_legacy("claude_code", lifecycle="dead")
        assert _candidate_for()["classification"] == lim.REFUSAL_TERMINAL_NOT_LIVE


# ---------------------------------------------------------------------------
# B/C. The one-candidate migration coordinator
# ---------------------------------------------------------------------------


class TestOneCandidateMigration:
    def test_migration_legacy_happy_path_links_every_seam(
        self, isolated_memory_db, harness, monkeypatch
    ):
        _seed_legacy("claude_code")
        _seed_roster("claude_code", generation=None)
        harness.screens.append(claude_panel_rows())
        harness.styled_screens.append(claude_composer_rows())

        # The durable repair-attempt-started marker is persisted BEFORE any
        # /status action (the spy runs before the repair's first byte).
        real_repair = lim.nsr.repair_terminal_native_identity
        observed: dict[str, Any] = {}

        def _spy(**kwargs: Any) -> dict[str, Any]:
            with database.SessionLocal() as db:
                row = db.query(database.LegacyIdentityMigrationModel).one()
                observed["status_at_invocation"] = row.status
                observed["repair_operation_id"] = row.repair_operation_id
            return real_repair(**kwargs)

        monkeypatch.setattr(lim.nsr, "repair_terminal_native_identity", _spy)

        outcome = _migration_call()

        assert outcome["status"] == lim.MIGRATION_MIGRATED
        assert outcome["reason"] is None
        assert outcome["native_session_id"] == SESSION_ID
        assert outcome["repair_status"] == nsr.STATUS_REPAIRED
        assert re.fullmatch(r"[0-9a-f]{64}", outcome["evidence_sha256"])
        assert outcome["task_bytes_submitted"] is False
        assert _typed_bytes(harness) == [("literal", "/status"), ("enter", ""), ("key", "Escape")]
        assert observed["status_at_invocation"] == lim.MIGRATION_ATTEMPT_STARTED

        # The repair's own durable seams are all linked.
        assert _legacy_row().native_session_id == SESSION_ID
        lineage = _current_lineage()
        assert lineage["native_session_id"] == SESSION_ID
        # Legacy occurrence split: the roster generation stays NULL while
        # the physical occurrence was the callback-target generation.
        incarnation = roster.get_incarnation_by_terminal(TERMINAL_ID, generation=None)
        assert incarnation["generation"] is None
        attachment = native_attachment.get("claude_code", SESSION_ID)
        assert attachment is not None
        assert attachment["state"] == native_attachment.ATTACHED
        assert attachment["owner"]["generation"] == CALLBACK_TARGET
        receipt = attachment["adoption_receipt"]
        assert receipt["operation_id"] == outcome["repair_operation_id"]
        assert receipt["evidence_sha256"] == outcome["evidence_sha256"]
        evidence = _evidence_rows()
        assert len(evidence) == 1
        assert evidence[0].operation_id == outcome["repair_operation_id"]
        assert evidence[0].generation == CALLBACK_TARGET
        assert evidence[0].native_session_id == SESSION_ID

        # One migration row: intent -> attempt-started -> migrated.
        rows = _migration_rows()
        assert len(rows) == 1
        row = rows[0]
        assert row.status == lim.MIGRATION_MIGRATED
        assert row.terminal_id == TERMINAL_ID
        assert row.physical_occurrence == CALLBACK_TARGET
        assert row.repair_operation_id == outcome["repair_operation_id"]
        assert row.audit_occurrence_id == outcome["audit_occurrence_id"]
        assert row.audit_candidate_digest == outcome["audit_candidate_digest"]
        assert row.evidence_sha256 == outcome["evidence_sha256"]
        assert row.native_session_id == SESSION_ID

    def test_migration_managed_exact_generation_happy_path(self, isolated_memory_db, harness):
        _seed_shared_managed("codex")
        _seed_roster("codex", generation=GENERATION)
        harness.screens.append(
            [
                ">_ OpenAI Codex (v0.147.0)",
                f"Session: {SESSION_ID}",
                "Model: gpt-5.4-codex",
                "cwd: /Users/x/repo",
            ]
        )
        outcome = _migration_call(provider_version="0.147.0")
        assert outcome["status"] == lim.MIGRATION_MIGRATED
        assert outcome["generation"] == GENERATION
        assert outcome["physical_occurrence"] == GENERATION
        assert outcome["native_session_id"] == SESSION_ID

    def test_migration_refuses_changed_digest_before_repair(self, isolated_memory_db, harness):
        _seed_legacy("claude_code")
        _seed_roster("claude_code", generation=None)
        harness.screens.append(claude_panel_rows())
        audit = lim.run_live_legacy_audit()
        candidate = next(
            c for c in audit["candidates"] if c["classification"] == lim.CANDIDATE_ELIGIBLE
        )
        outcome = _call_with_candidate(candidate, _uuid(), audit_candidate_digest="f" * 64)
        assert outcome["status"] == lim.MIGRATION_REFUSED
        assert outcome["reason"] == "candidate-drift"
        assert harness.typed == []
        assert _evidence_rows() == []
        assert _legacy_row().native_session_id is None

    def test_migration_refuses_changed_terminal_facts_before_repair(
        self, isolated_memory_db, harness
    ):
        _seed_legacy("claude_code")
        _seed_roster("claude_code", generation=None)
        candidate = _candidate_for()
        outcome = _call_with_candidate(candidate, _uuid(), provider="codex")
        assert outcome["status"] == lim.MIGRATION_REFUSED
        assert outcome["reason"] == "provider-drift"
        assert harness.typed == []

    def test_migration_exact_retry_adopts_without_second_status(self, isolated_memory_db, harness):
        _seed_legacy("claude_code")
        _seed_roster("claude_code", generation=None)
        harness.screens.append(claude_panel_rows())
        harness.styled_screens.append(claude_composer_rows())
        audit = lim.run_live_legacy_audit()
        candidate = next(
            c for c in audit["candidates"] if c["classification"] == lim.CANDIDATE_ELIGIBLE
        )
        operation_id = _uuid()
        first = _call_with_candidate(candidate, operation_id)
        assert first["status"] == lim.MIGRATION_MIGRATED
        assert _typed_bytes(harness) == [("literal", "/status"), ("enter", ""), ("key", "Escape")]
        assert len(_evidence_rows()) == 1

        # Response loss: an exact duplicate queries the SAME migration and
        # repair operations and never triggers a second status interaction.
        second = _call_with_candidate(candidate, operation_id)
        assert second["status"] == lim.MIGRATION_MIGRATED
        assert second["repair_operation_id"] == first["repair_operation_id"]
        assert second["evidence_sha256"] == first["evidence_sha256"]
        assert _typed_bytes(harness) == [("literal", "/status"), ("enter", ""), ("key", "Escape")]
        assert len(_evidence_rows()) == 1
        assert len(_migration_rows()) == 1

    def test_migration_changed_request_same_id_conflicts_before_repair(
        self, isolated_memory_db, harness
    ):
        _seed_legacy("claude_code")
        _seed_roster("claude_code", generation=None)
        harness.screens.append(claude_panel_rows())
        harness.styled_screens.append(claude_composer_rows())
        candidate = _candidate_for()
        operation_id = _uuid()
        first = _call_with_candidate(candidate, operation_id)
        assert first["status"] == lim.MIGRATION_MIGRATED

        before = _typed_bytes(harness)
        conflict = _call_with_candidate(candidate, operation_id, provider_version="9.9.9")
        assert conflict["status"] == lim.MIGRATION_REFUSED
        assert conflict["reason"] == "operation-conflict"
        assert _typed_bytes(harness) == before
        assert len(_evidence_rows()) == 1
        assert len(_migration_rows()) == 1

    def test_migration_refuses_when_identity_became_known(self, isolated_memory_db, harness):
        _seed_legacy("claude_code", native_session_id=SESSION_ID)
        _seed_roster("claude_code", generation=None)
        audit = lim.run_live_legacy_audit()
        candidate = next(
            c for c in audit["candidates"] if c["classification"] == lim.REFUSAL_ALREADY_KNOWN
        )
        outcome = _call_with_candidate(candidate, _uuid())
        assert outcome["status"] == lim.MIGRATION_REFUSED
        assert outcome["reason"] == "already-known"
        assert harness.typed == []
        assert _legacy_row().native_session_id == SESSION_ID
        assert _evidence_rows() == []

    def test_migration_injected_failure_leaves_truthful_resumable_state(
        self, isolated_memory_db, harness
    ):
        _seed_legacy("claude_code")
        _seed_roster("claude_code", generation=None)
        # The audit itself must run healthy; the failure is injected only
        # into the migration's revalidation/repair window.
        candidate = _candidate_for()
        harness.pane_identity_error = RuntimeError("pane vanished")
        outcome = _call_with_candidate(candidate, _uuid())
        assert outcome["status"] == lim.MIGRATION_REFUSED
        assert outcome["reason"] == "unknown-liveness"
        assert _legacy_row().native_session_id is None
        assert _evidence_rows() == []
        rows = _migration_rows()
        assert len(rows) == 1
        assert rows[0].status == lim.MIGRATION_REFUSED

    def test_attempt_started_without_evidence_is_unresolved_and_never_resends(
        self, isolated_memory_db, harness
    ):
        """Response loss after attempt-started with no repair evidence: typed
        unresolved, zero bytes, and the row stays truthfully in flight."""
        _seed_legacy("claude_code")
        _seed_roster("claude_code", generation=None)
        harness.screens.append(claude_panel_rows())
        harness.styled_screens.append(claude_composer_rows())

        candidate = _candidate_for()
        operation_id = _uuid()
        req_digest = lim.migration_request_digest(
            terminal_id=candidate["terminal_id"],
            provider=candidate["provider"],
            generation=None,
            physical_occurrence=candidate["physical_occurrence"],
            provider_version=CLAUDE_VERSION,
            audit_occurrence_id=candidate["occurrence_id"],
            audit_candidate_digest=candidate["evidence_digest"],
        )
        repair_op = str(uuid.uuid4())
        _intent_row(
            operation_id=operation_id,
            request_digest=req_digest,
            candidate=candidate,
            repair_operation_id=repair_op,
            status=lim.MIGRATION_ATTEMPT_STARTED,
        )

        outcome = _call_with_candidate(candidate, operation_id)
        assert outcome["status"] == lim.MIGRATION_REFUSED
        assert outcome["reason"] == "repair-attempt-unresolved"
        assert harness.typed == []
        assert _evidence_rows() == []
        assert _legacy_row().native_session_id is None
        rows = _migration_rows()
        assert len(rows) == 1
        assert rows[0].status == lim.MIGRATION_ATTEMPT_STARTED

    def test_attempt_started_with_partial_attachment_is_ambiguous(
        self, isolated_memory_db, harness
    ):
        """A conservative adoption without committed evidence is ambiguous,
        never a resend and never a silent success."""
        _seed_legacy("claude_code")
        _seed_roster("claude_code", generation=None)
        harness.screens.append(claude_panel_rows())

        candidate = _candidate_for()
        operation_id = _uuid()
        req_digest = lim.migration_request_digest(
            terminal_id=candidate["terminal_id"],
            provider=candidate["provider"],
            generation=None,
            physical_occurrence=candidate["physical_occurrence"],
            provider_version=CLAUDE_VERSION,
            audit_occurrence_id=candidate["occurrence_id"],
            audit_candidate_digest=candidate["evidence_digest"],
        )
        repair_op = str(uuid.uuid4())
        _intent_row(
            operation_id=operation_id,
            request_digest=req_digest,
            candidate=candidate,
            repair_operation_id=repair_op,
            status=lim.MIGRATION_ATTEMPT_STARTED,
        )
        receipt = native_attachment.status_repair_adoption_receipt(
            operation_id=repair_op,
            request_digest=req_digest,
            provider="claude_code",
            native_session_id=SESSION_ID,
            terminal_id=TERMINAL_ID,
            generation=CALLBACK_TARGET,
            execution_mode=em.NATIVE_TUI,
            pane_id=PANE_ID,
            process_identity={"pid": PANE_PID, "start_marker": START_MARKER},
            parser_key=nsr.PARSER_CLAUDE_MODAL,
            provider_version=CLAUDE_VERSION,
            evidence_sha256="b" * 64,
            observed_at="2026-08-10T00:00:00Z",
            composer_restored=True,
        )
        _seed_attachment(adoption_receipt=receipt)

        outcome = _call_with_candidate(candidate, operation_id)
        assert outcome["status"] == lim.MIGRATION_REFUSED
        assert outcome["reason"] == "repair-attempt-ambiguous"
        assert harness.typed == []
        assert _evidence_rows() == []
        assert _legacy_row().native_session_id is None

    def test_attempt_started_with_evidence_adopts_without_resend(self, isolated_memory_db, harness):
        """A crash between repair commit and the migration record derives
        completion from the repair evidence on retry — no second /status."""
        _seed_legacy("claude_code")
        _seed_roster("claude_code", generation=None)
        harness.screens.append(claude_panel_rows())
        harness.styled_screens.append(claude_composer_rows())

        candidate = _candidate_for()
        operation_id = _uuid()
        first = _call_with_candidate(candidate, operation_id)
        assert first["status"] == lim.MIGRATION_MIGRATED
        assert _typed_bytes(harness) == [("literal", "/status"), ("enter", ""), ("key", "Escape")]
        assert len(_evidence_rows()) == 1

        # Simulate the crash: the row never left attempt-started.
        with database.SessionLocal() as db:
            row = db.query(database.LegacyIdentityMigrationModel).one()
            row.status = lim.MIGRATION_ATTEMPT_STARTED
            row.outcome_json = None
            db.commit()

        adopted = _call_with_candidate(candidate, operation_id)
        assert adopted["status"] == lim.MIGRATION_MIGRATED
        assert adopted["repair_operation_id"] == first["repair_operation_id"]
        assert adopted["evidence_sha256"] == first["evidence_sha256"]
        assert adopted["native_session_id"] == SESSION_ID
        assert _typed_bytes(harness) == [("literal", "/status"), ("enter", ""), ("key", "Escape")]
        assert len(_evidence_rows()) == 1
        rows = _migration_rows()
        assert len(rows) == 1
        assert rows[0].status == lim.MIGRATION_MIGRATED


# ---------------------------------------------------------------------------
# E. Batch iteration and rollback
# ---------------------------------------------------------------------------


SESSION_ID_2 = "5f5f46c7-b660-4f6f-a144-d2c6dceccf95"


class TestBatchAndRollback:
    def test_batch_iteration_stops_truthfully(self, isolated_memory_db, harness, monkeypatch):
        _seed_legacy("claude_code", terminal_id="m1")
        _seed_roster("claude_code", terminal_id="m1", generation=None)
        _seed_legacy("claude_code", terminal_id="m2")
        _seed_roster("claude_code", terminal_id="m2", generation=None)
        _seed_legacy("kiro_cli", terminal_id="k1")
        # Each migrated terminal's pane renders its OWN session id (the
        # attachment store is keyed by (provider, session)).
        panels = iter(
            [claude_panel_rows(session_id=SESSION_ID), claude_panel_rows(session_id=SESSION_ID_2)]
        )

        def _capture(pane_id: str, **_kwargs: Any) -> list[str]:
            return list(next(panels))

        monkeypatch.setattr(npi, "capture_pane_screen", _capture)
        harness.styled_screens.append(claude_composer_rows())

        audit = lim.run_live_legacy_audit()
        by_terminal = {c["terminal_id"]: c for c in audit["candidates"]}
        requests = []
        for tid in ("m1", "m2"):
            c = by_terminal[tid]
            requests.append(
                {
                    "operation_id": _uuid(),
                    "terminal_id": c["terminal_id"],
                    "provider": c["provider"],
                    "generation": c["generation"],
                    "physical_occurrence": c["physical_occurrence"],
                    "provider_version": CLAUDE_VERSION,
                    "audit_occurrence_id": c["occurrence_id"],
                    "audit_candidate_digest": c["evidence_digest"],
                }
            )
        k = by_terminal["k1"]
        requests.append(
            {
                "operation_id": _uuid(),
                "terminal_id": k["terminal_id"],
                "provider": k["provider"],
                "generation": None,
                "physical_occurrence": k["physical_occurrence"],
                "provider_version": None,
                "audit_occurrence_id": k["occurrence_id"],
                "audit_candidate_digest": k["evidence_digest"],
            }
        )
        batch = lim.iterate_migration_candidates(requests)
        assert batch["schema"] == lim.MIGRATION_BATCH_SCHEMA
        assert batch["stopped"] is True
        assert batch["stopped_after"] == 2
        assert batch["results"][0]["status"] == lim.MIGRATION_MIGRATED
        assert batch["results"][1]["status"] == lim.MIGRATION_MIGRATED
        assert batch["results"][2]["status"] == lim.MIGRATION_REFUSED
        assert batch["results"][2]["reason"] == "unsupported-provider"
        assert _legacy_row("m1").native_session_id == SESSION_ID
        assert _legacy_row("m2").native_session_id == SESSION_ID_2
        # Intent-first: the refused candidate also leaves a durable row.
        assert len(_migration_rows()) == 3

    def test_rollback_disables_new_work_and_retains_prior_rows(
        self, isolated_memory_db, harness, monkeypatch
    ):
        _seed_legacy("claude_code")
        _seed_roster("claude_code", generation=None)
        harness.screens.append(claude_panel_rows())
        harness.styled_screens.append(claude_composer_rows())
        first = _migration_call()
        assert first["status"] == lim.MIGRATION_MIGRATED
        prior_rows = len(_migration_rows())

        monkeypatch.setenv("CAO_LEGACY_MIGRATION_PRODUCER_ENABLED", "0")
        assert lim.migration_producer_enabled() is False

        # A NEW operation is refused (the terminal is already enrolled, so
        # the request is built from the first operation's recorded facts).
        refused = lim.migrate_terminal_native_identity(
            operation_id=_uuid(),
            terminal_id=first["terminal_id"],
            provider=first["provider"],
            generation=first["generation"],
            physical_occurrence=first["physical_occurrence"],
            provider_version=CLAUDE_VERSION,
            audit_occurrence_id=first["audit_occurrence_id"],
            audit_candidate_digest=first["audit_candidate_digest"],
        )
        assert refused["status"] == lim.MIGRATION_REFUSED
        assert refused["reason"] == "producer-disabled"
        assert _typed_bytes(harness) == [("literal", "/status"), ("enter", ""), ("key", "Escape")]

        assert len(_migration_rows()) == prior_rows
        adopted = lim.migrate_terminal_native_identity(
            operation_id=first["operation_id"],
            terminal_id=first["terminal_id"],
            provider=first["provider"],
            generation=first["generation"],
            physical_occurrence=first["physical_occurrence"],
            provider_version=CLAUDE_VERSION,
            audit_occurrence_id=first["audit_occurrence_id"],
            audit_candidate_digest=first["audit_candidate_digest"],
        )
        assert adopted["status"] == lim.MIGRATION_MIGRATED
        assert adopted["repair_operation_id"] == first["repair_operation_id"]
        assert _typed_bytes(harness) == [("literal", "/status"), ("enter", ""), ("key", "Escape")]
        assert len(_migration_rows()) == prior_rows

    def test_old_roster_readers_ignore_additive_migration_rows(self, isolated_memory_db, harness):
        _seed_legacy("claude_code")
        _seed_roster("claude_code", generation=None)
        harness.screens.append(claude_panel_rows())
        harness.styled_screens.append(claude_composer_rows())
        _migration_call()
        roster_audit = roster.audit_dry_run()
        assert roster_audit["schema"] == "cao-m3-roster-audit-v1"
        assert len(roster.list_agents()) == 1
        assert roster.list_lineages()[0]["native_session_id"] == SESSION_ID


# ---------------------------------------------------------------------------
# Gate fixes: at-most-once status, committed-evidence authority, Kimi
# self-heal, build provenance (red first)
# ---------------------------------------------------------------------------


class TestAtMostOnceStatusAction:
    def test_concurrent_exact_duplicates_send_status_at_most_once(
        self, isolated_memory_db, harness, monkeypatch
    ):
        """Two exact callers racing on one operation: exactly one may ever
        type /status.  Caller 1 is released only after caller 2 has already
        entered the repair; caller 2 wins the execution claim."""
        import threading

        _seed_legacy("claude_code")
        _seed_roster("claude_code", generation=None)
        harness.screens.append(claude_panel_rows())
        harness.styled_screens.append(claude_composer_rows())

        def _always_fail_capture(pane_id: str, **_kwargs: Any) -> list[str]:
            raise RuntimeError("capture failed")

        monkeypatch.setattr(npi, "capture_pane_screen", _always_fail_capture)

        real_persist = lim._persist_migration_intent
        persisted = threading.Event()
        release_persist = threading.Event()

        def _spy_persist(**kwargs: Any) -> bool:
            result = real_persist(**kwargs)
            persisted.set()
            release_persist.wait(timeout=60)
            return result

        monkeypatch.setattr(lim, "_persist_migration_intent", _spy_persist)

        real_repair = lim.nsr.repair_terminal_native_identity
        repair_entered = threading.Event()
        release_repair = threading.Event()

        def _spy_repair(**kwargs: Any) -> dict[str, Any]:
            repair_entered.set()
            release_repair.wait(timeout=60)
            return real_repair(**kwargs)

        monkeypatch.setattr(lim.nsr, "repair_terminal_native_identity", _spy_repair)

        candidate = _candidate_for()
        operation_id = _uuid()
        results: dict[str, Any] = {}

        def _caller(name: str) -> None:
            results[name] = _call_with_candidate(candidate, operation_id)

        t1 = threading.Thread(target=_caller, args=("one",))
        t1.start()
        assert persisted.wait(timeout=30)
        t2 = threading.Thread(target=_caller, args=("two",))
        t2.start()
        assert repair_entered.wait(timeout=30)
        release_persist.set()
        t1.join(timeout=3)
        # Caller 2 entered the repair while caller 1 was still inside; with
        # the atomic execution claim exactly one of them proceeds to the
        # status action.  Release the repair gate and let both finish.
        release_repair.set()
        t1.join(timeout=30)
        t2.join(timeout=30)

        status_count = sum(
            1
            for entry in harness.typed
            if entry["kind"] == "literal" and entry["text"] == "/status"
        )
        assert status_count == 1, harness.typed
        assert len(_migration_rows()) == 1

    def test_repair_exact_retry_after_failed_observation_never_resends(
        self, isolated_memory_db, harness, monkeypatch
    ):
        """The observation-attempt journal lives at PR #99's byte seam: an
        exact retry of an operation whose /status failed before a verdict is
        typed ambiguous and never sends /status again."""
        _seed_legacy("claude_code")
        _seed_roster("claude_code", generation=None)
        harness.screens.append(claude_panel_rows())
        harness.styled_screens.append(claude_composer_rows())

        def _always_fail_capture(pane_id: str, **_kwargs: Any) -> list[str]:
            raise RuntimeError("capture failed")

        monkeypatch.setattr(npi, "capture_pane_screen", _always_fail_capture)

        op = _uuid()
        first = lim.nsr.repair_terminal_native_identity(
            terminal_id=TERMINAL_ID,
            generation=None,
            provider_version=CLAUDE_VERSION,
            physical_occurrence=CALLBACK_TARGET,
            operation_id=op,
        )
        assert first["status"] == nsr.STATUS_REFUSED
        count_after_first = sum(
            1
            for entry in harness.typed
            if entry["kind"] == "literal" and entry["text"] == "/status"
        )
        assert count_after_first == 1

        # An unsafe retry would send again — the journal must refuse it
        # instead, with zero new bytes.
        second = lim.nsr.repair_terminal_native_identity(
            terminal_id=TERMINAL_ID,
            generation=None,
            provider_version=CLAUDE_VERSION,
            physical_occurrence=CALLBACK_TARGET,
            operation_id=op,
        )
        assert second["status"] == nsr.STATUS_REFUSED
        assert second["reason"] == "observation-attempt-ambiguous"
        count_after_second = sum(
            1
            for entry in harness.typed
            if entry["kind"] == "literal" and entry["text"] == "/status"
        )
        assert count_after_second == count_after_first
        assert _legacy_row().native_session_id is None


class TestCommittedEvidenceAuthority:
    def test_committed_evidence_wins_over_later_pane_loss(
        self, isolated_memory_db, harness, monkeypatch
    ):
        """A committed PR #99 repair is the authoritative migrated truth; a
        pane that exits AFTER the commit is a separate lifecycle fact and
        never downgrades the recorded outcome."""
        _seed_legacy("claude_code")
        _seed_roster("claude_code", generation=None)
        harness.screens.append(claude_panel_rows())
        harness.styled_screens.append(claude_composer_rows())

        real_repair = lim.nsr.repair_terminal_native_identity

        def _teardown_after(**kwargs: Any) -> dict[str, Any]:
            outcome = real_repair(**kwargs)
            harness.pane_identity = None  # the pane exits after the commit
            harness.live_start_marker = None
            return outcome

        monkeypatch.setattr(lim.nsr, "repair_terminal_native_identity", _teardown_after)

        outcome = _migration_call()
        assert outcome["status"] == lim.MIGRATION_MIGRATED
        assert outcome["evidence_sha256"]
        rows = _migration_rows()
        assert len(rows) == 1
        assert rows[0].status == lim.MIGRATION_MIGRATED
        assert _legacy_row().native_session_id == SESSION_ID


class TestKimiSelfHeal:
    def test_audit_kimi_candidate_is_eligible_with_unknown_session_state(
        self, isolated_memory_db, harness
    ):
        _seed_legacy("kimi_cli")
        _seed_roster("kimi_cli", generation=None)
        candidate = _candidate_for()
        assert candidate["classification"] == lim.CANDIDATE_ELIGIBLE
        assert candidate["session_probe_required"] is True

    def test_kimi_pristine_panel_is_identity_still_missing_without_turn(
        self, isolated_memory_db, harness
    ):
        _seed_legacy("kimi_cli")
        _seed_roster("kimi_cli", generation=None)
        harness.screens.append(
            [
                "╭────────────────────────────────────────────╮",
                "│ >_ Kimi Code (v0.34.0)",
                "│ Model: kimi-k2",
                "│ Session none",
                "╰────────────────────────────────────────────╯",
            ]
        )
        candidate = _candidate_for()
        operation_id = _uuid()
        outcome = _call_with_candidate(candidate, operation_id, provider_version="0.34.0")

        assert outcome["status"] == lim.MIGRATION_IDENTITY_STILL_MISSING
        assert outcome["native_session_id"] is None
        assert _typed_bytes(harness) == [("literal", "/status"), ("enter", "")]
        # No task/bootstrap turn of any kind.
        assert harness.typed == [
            {"kind": "literal", "text": "/status"},
            {"kind": "enter"},
        ]
        assert _legacy_row().native_session_id is None
        assert _evidence_rows() == []
        # The journal makes the verdict replayable without a second /status.
        journal = lim.nsr.repair_observation_attempt(outcome["repair_operation_id"])
        assert journal is not None
        assert journal["status"] == "identity-still-missing"
        assert journal["status_action_count"] == 1
        replay = _call_with_candidate(candidate, operation_id, provider_version="0.34.0")
        assert replay["status"] == lim.MIGRATION_IDENTITY_STILL_MISSING
        assert replay["repair_operation_id"] == outcome["repair_operation_id"]
        assert _typed_bytes(harness) == [("literal", "/status"), ("enter", "")]

    def test_kimi_post_task_panel_migrates(self, isolated_memory_db, harness):
        """A Kimi pane that already processed a real task lazily created its
        session; /status exposes it and the migration repairs it."""
        _seed_legacy("kimi_cli")
        _seed_roster("kimi_cli", generation=None)
        harness.screens.append(
            [
                "╭────────────────────────────────────────────╮",
                "│ >_ Kimi Code (v0.34.0)",
                "│ Model: kimi-k2",
                "│ Session session_4f5f46c7-b660-4f6f-a144-d2c6dceccf95",
                "╰────────────────────────────────────────────╯",
            ]
        )
        outcome = _migration_call(provider_version="0.34.0")
        assert outcome["status"] == lim.MIGRATION_MIGRATED
        assert outcome["native_session_id"] == "session_4f5f46c7-b660-4f6f-a144-d2c6dceccf95"
        assert _legacy_row().native_session_id == "session_4f5f46c7-b660-4f6f-a144-d2c6dceccf95"
        journal = lim.nsr.repair_observation_attempt(outcome["repair_operation_id"])
        assert journal["status"] == "observed"
        assert journal["status_action_count"] == 1


class TestOccurrenceAndBuildProvenance:
    def test_missing_occurrence_refuses_before_intent(self, isolated_memory_db, harness):
        _seed_legacy("claude_code", callback_target=None)
        audit = lim.run_live_legacy_audit()
        candidate = next(c for c in audit["candidates"] if c["terminal_id"] == TERMINAL_ID)
        outcome = lim.migrate_terminal_native_identity(
            operation_id=_uuid(),
            terminal_id=candidate["terminal_id"],
            provider=candidate["provider"],
            generation=None,
            physical_occurrence=None,
            provider_version=CLAUDE_VERSION,
            audit_occurrence_id=candidate["occurrence_id"],
            audit_candidate_digest=candidate["evidence_digest"],
        )
        assert outcome["status"] == lim.MIGRATION_REFUSED
        assert outcome["reason"] == "missing-occurrence"
        assert harness.typed == []
        # No intent row was ever inserted (no raw integrity error surfaced).
        assert _migration_rows() == []

    def test_refused_retry_returns_recorded_without_io(self, isolated_memory_db, harness):
        _seed_legacy("claude_code")
        _seed_roster("claude_code", generation=None)
        harness.pane_identity_error = RuntimeError("pane vanished")
        candidate = _candidate_for()
        operation_id = _uuid()
        first = _call_with_candidate(candidate, operation_id)
        assert first["status"] == lim.MIGRATION_REFUSED

        before = list(harness.typed)
        retry = _call_with_candidate(candidate, operation_id)
        assert retry["status"] == lim.MIGRATION_REFUSED
        assert retry["reason"] == first["reason"]
        assert list(harness.typed) == before
        assert len(_migration_rows()) == 1

    def test_build_provenance_is_bound_in_digest_and_migration(self, isolated_memory_db, harness):
        _seed_legacy("claude_code")
        _seed_roster("claude_code", generation=None)
        harness.screens.append(claude_panel_rows())
        harness.styled_screens.append(claude_composer_rows())

        candidate = _candidate_for()
        provenance = candidate["build_provenance"]
        assert provenance["source"] == "pinned-legacy-plan-fallback"
        assert provenance["observed"] is False
        assert provenance["provider_version"] is None
        # The provenance is bound into the candidate digest.
        altered = dict(candidate)
        altered["build_provenance"] = {
            "source": "managed-v2-binding",
            "provider_version": "2.1.226",
            "observed": True,
        }
        assert lim.candidate_evidence_digest(altered) != candidate["evidence_digest"]

        outcome = _call_with_candidate(candidate, _uuid())
        assert outcome["status"] == lim.MIGRATION_MIGRATED
        assert outcome["build_provenance"]["source"] == "pinned-legacy-plan-fallback"
        # The caller-provided version is plan selection only, never described
        # as observed build proof.
        assert outcome["provider_version"] == CLAUDE_VERSION
        assert outcome["build_provenance"]["observed"] is False


class TestObservationJournalExactness:
    def test_failure_after_enter_journal_reports_submitted_not_zero_action(
        self, isolated_memory_db, harness, monkeypatch
    ):
        """A successfully submitted /status followed by capture/parse loss is
        journaled as submitted with action count 1 — never a false
        zero-action fact — and an exact retry sends nothing."""
        _seed_legacy("claude_code")
        _seed_roster("claude_code", generation=None)
        harness.screens.append(claude_panel_rows())
        harness.styled_screens.append(claude_composer_rows())

        def _always_fail_capture(pane_id: str, **_kwargs: Any) -> list[str]:
            raise RuntimeError("capture failed")

        monkeypatch.setattr(npi, "capture_pane_screen", _always_fail_capture)

        op = _uuid()
        first = lim.nsr.repair_terminal_native_identity(
            terminal_id=TERMINAL_ID,
            generation=None,
            provider_version=CLAUDE_VERSION,
            physical_occurrence=CALLBACK_TARGET,
            operation_id=op,
        )
        assert first["status"] == nsr.STATUS_REFUSED
        assert first["reason"] == "panel-unparsed"

        journal = lim.nsr.repair_observation_attempt(op)
        assert journal is not None
        # The Enter succeeded: the observation WAS submitted.  The journal
        # must not describe zero action.
        assert journal["status"] == nsr.OBSERVATION_SUBMITTED
        assert journal["status_action_count"] == 1

        count_before = sum(
            1
            for entry in harness.typed
            if entry["kind"] == "literal" and entry["text"] == "/status"
        )
        second = lim.nsr.repair_terminal_native_identity(
            terminal_id=TERMINAL_ID,
            generation=None,
            provider_version=CLAUDE_VERSION,
            physical_occurrence=CALLBACK_TARGET,
            operation_id=op,
        )
        assert second["status"] == nsr.STATUS_REFUSED
        assert second["reason"] == "observation-attempt-ambiguous"
        count_after = sum(
            1
            for entry in harness.typed
            if entry["kind"] == "literal" and entry["text"] == "/status"
        )
        assert count_after == count_before == 1
        assert _legacy_row().native_session_id is None
