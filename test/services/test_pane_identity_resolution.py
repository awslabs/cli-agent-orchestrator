"""Bounded exact-live-pane identity resolution (cond-0377D M3-A read seam).

Resolves one exact live tmux pane to its registered CAO terminal, unique
LIVE stable-agent incarnation, and stable agent/lineage identity — the
fork primitive the conductor's ``conduct whoami`` will consume.  Strictly
read-only and deterministic cooperative-local routing: caller-supplied
terminal ids or environment labels never override the exact pane mapping;
an unregistered, dead, unreadable, stale, superseded, or ambiguous pane is
a typed non-identity, never a guessed identity.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Optional

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.tmux import PaneControlIdentity, TmuxClient
from cli_agent_orchestrator.services import execution_mode as em
from cli_agent_orchestrator.services import pane_identity_resolution as pir
from cli_agent_orchestrator.services import stable_agent_roster as roster

TERMINAL_ID = "a1b2c3d4"
GENERATION = "00000000-0000-4000-8000-000000000001"
CALLBACK_TARGET = "00000000-0000-4000-8000-0000000000aa"
PANE_ID = "%7"
WINDOW_ID = "@7"
TMUX_SESSION_ID = "$1"
SERVER_SOCKET = "/private/tmp/cao-native.sock"
SERVER_SOCKET_OTHER = "/private/tmp/cao-other.sock"
PANE_PID = 4242
START_MARKER = "Thu Jul 24 10:00:00 2026"
SESSION_NAME = "cao-campaign"
AGENT_ID = "11111111-1111-4111-8111-111111111111"
NATIVE_SESSION_ID = "4f5f46c7-b660-4f6f-a144-d2c6dceccf95"


def _uuid() -> str:
    return str(uuid.uuid4())


class _PaneHarness:
    def __init__(self) -> None:
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
        self.calls: list[str] = []

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


@pytest.fixture
def harness(monkeypatch):
    state = _PaneHarness()
    monkeypatch.setattr(TmuxClient, "pane_control_identity", state.pane_control_identity)
    monkeypatch.setattr(TmuxClient, "observe_pane_server_identity", state.pane_server_identity)
    monkeypatch.setattr(pir, "_live_start_marker", state.start_marker)
    return state


def _seed_legacy(
    *,
    terminal_id: str = TERMINAL_ID,
    lifecycle: str = "live",
    pane_id: str = PANE_ID,
    window_id: str = WINDOW_ID,
    session_id: str = TMUX_SESSION_ID,
    pane_pid: int = PANE_PID,
    server_socket: str = SERVER_SOCKET,
) -> None:
    with database.SessionLocal() as db:
        db.add(
            database.TerminalModel(
                id=terminal_id,
                tmux_session=SESSION_NAME,
                tmux_window=f"w-{terminal_id}",
                provider="claude_code",
                generation=None,
                callback_target_generation=CALLBACK_TARGET,
                pane_id=pane_id,
                window_id=window_id,
                server_socket_path=server_socket,
                session_id=session_id,
                pane_pid=pane_pid,
                native_session_id=None,
                lifecycle_state=lifecycle,
            )
        )
        db.commit()


def _seed_managed(
    *,
    terminal_id: str = TERMINAL_ID,
    generation: str = GENERATION,
    lifecycle: str = "live",
    pane_id: str = PANE_ID,
    window_id: str = WINDOW_ID,
    session_id: str = TMUX_SESSION_ID,
    pane_pid: int = PANE_PID,
    server_socket: str = SERVER_SOCKET,
) -> None:
    with database.SessionLocal() as db:
        db.add(
            database.TerminalModel(
                id=terminal_id,
                tmux_session=SESSION_NAME,
                tmux_window=f"w-{terminal_id}",
                provider="claude_code",
                generation=generation,
                callback_target_generation=None,
                pane_id=pane_id,
                window_id=window_id,
                server_socket_path=server_socket,
                session_id=session_id,
                pane_pid=pane_pid,
                native_session_id=None,
                lifecycle_state=lifecycle,
            )
        )
        db.commit()


def _seed_roster(
    *,
    terminal_id: str = TERMINAL_ID,
    generation: Optional[str] = GENERATION,
    native_session_id: Optional[str] = NATIVE_SESSION_ID,
    agent_id: str = AGENT_ID,
    pane_id: str = PANE_ID,
    pane_pid: int = PANE_PID,
) -> dict[str, Any]:
    return roster.bind_generation(
        roster.BindingContract(
            agent_id=agent_id,
            session_name=SESSION_NAME,
            role=roster.ROLE_WORKER,
            profile_family="developer",
            harness="claude_code",
            native_session_id=native_session_id,
            terminal_id=terminal_id,
            generation=generation,
            pane_id=pane_id,
            pane_pid=pane_pid,
            process_identity={"pid": pane_pid, "start_marker": START_MARKER},
            execution_mode=em.NATIVE_TUI,
        )
    )


def _resolve(**changes: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"pane_id": PANE_ID, "server_socket_path": SERVER_SOCKET}
    payload.update(changes)
    return pir.resolve_pane_identity(**payload)


def _dump_all_rows() -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    with database.SessionLocal() as db:
        for table in database.Base.metadata.sorted_tables:
            rows = db.execute(table.select()).all()
            snapshot[table.name] = sorted(
                (dict(row._mapping) for row in rows),
                key=lambda r: json.dumps(r, sort_keys=True, default=str),
            )
    return snapshot


class TestPaneResolution:
    def test_exact_live_registered_pane_resolves_terminal_incarnation_agent(
        self, isolated_memory_db, harness
    ):
        _seed_managed()
        _seed_roster()

        outcome = _resolve()

        assert outcome["schema"] == pir.PANE_RESOLUTION_SCHEMA
        assert outcome["status"] == "resolved"
        assert outcome["reason"] is None
        assert outcome["pane"] == {
            "pane_id": PANE_ID,
            "window_id": WINDOW_ID,
            "session_id": TMUX_SESSION_ID,
            "pane_pid": PANE_PID,
            "server_socket_path": SERVER_SOCKET,
        }
        assert outcome["terminal"]["terminal_id"] == TERMINAL_ID
        assert outcome["terminal"]["generation"] == GENERATION
        assert outcome["terminal"]["physical_occurrence"] == GENERATION
        assert outcome["incarnation"]["incarnation_id"]
        assert outcome["incarnation"]["disposition"] == roster.INCARNATION_BOUND
        assert outcome["agent"]["agent_id"] == AGENT_ID
        assert outcome["agent"]["lineage_id"] == outcome["incarnation"]["lineage_id"]
        assert outcome["agent"]["harness"] == "claude_code"
        assert outcome["agent"]["native_session_id"] == NATIVE_SESSION_ID

    def test_identical_pane_id_on_two_servers_cannot_cross_resolve(
        self, isolated_memory_db, harness
    ):
        _seed_managed()
        _seed_roster()
        # The pane is observed live on THIS server; the caller claims a
        # different canonical server -> the same pane id cannot resolve.
        outcome = _resolve(server_socket_path=SERVER_SOCKET_OTHER)
        assert outcome["status"] == "pane-unreadable-or-dead"
        assert outcome["terminal"] is None
        assert outcome["incarnation"] is None
        assert outcome["agent"] is None

    def test_reused_pane_id_with_changed_pid_refuses_as_stale(self, isolated_memory_db, harness):
        _seed_managed()
        _seed_roster()
        # The registered row claims pane %7 with pid 4242; a new pane reused
        # %7 with a different pid -> the row is stale, never the worker.
        harness.pane_identity = PaneControlIdentity(
            pane_id=PANE_ID,
            window_id=WINDOW_ID,
            session_id=TMUX_SESSION_ID,
            pane_pid=9999,
            session_name=SESSION_NAME,
            window_name=f"w-{TERMINAL_ID}",
            bracketed_paste_proven=False,
            dead=False,
            server_socket_path=SERVER_SOCKET,
        )
        outcome = _resolve()
        assert outcome["status"] == "terminal-pane-mismatch-or-superseded"
        assert outcome["agent"] is None

    def test_dead_and_unreadable_panes_are_typed_non_identity(self, isolated_memory_db, harness):
        _seed_managed()
        _seed_roster()
        harness.pane_identity = PaneControlIdentity(
            pane_id=PANE_ID,
            window_id=WINDOW_ID,
            session_id=TMUX_SESSION_ID,
            pane_pid=PANE_PID,
            session_name=SESSION_NAME,
            window_name=f"w-{TERMINAL_ID}",
            bracketed_paste_proven=False,
            dead=True,
            server_socket_path=SERVER_SOCKET,
        )
        assert _resolve()["status"] == "pane-unreadable-or-dead"

        harness.pane_identity = None
        assert _resolve()["status"] == "pane-unreadable-or-dead"

        harness.pane_identity_error = RuntimeError("tmux unreachable")
        assert _resolve()["status"] == "pane-unreadable-or-dead"

    def test_unregistered_pane_returns_no_identity(self, isolated_memory_db, harness):
        # The pane is live on the right server but no terminal row claims it.
        assert _resolve()["status"] == "pane-unregistered"
        assert _resolve()["terminal"] is None

    def test_historical_retired_incarnation_cannot_resolve(self, isolated_memory_db, harness):
        _seed_managed()
        _seed_roster()
        roster.retire_incarnation(terminal_id=TERMINAL_ID, generation=GENERATION, reason="done")

        outcome = _resolve()
        assert outcome["status"] == "roster-incarnation-missing"
        assert outcome["agent"] is None

    def test_null_generation_legacy_resolves_through_unique_live_occurrence(
        self, isolated_memory_db, harness
    ):
        _seed_legacy()
        _seed_roster(generation=None)

        outcome = _resolve()
        assert outcome["status"] == "resolved"
        assert outcome["terminal"]["generation"] is None
        assert outcome["terminal"]["physical_occurrence"] == CALLBACK_TARGET
        assert outcome["incarnation"]["disposition"] == roster.INCARNATION_BOUND
        assert outcome["agent"]["agent_id"] == AGENT_ID

    def test_ambiguous_legacy_incarnations_refuse_without_picking(
        self, isolated_memory_db, harness
    ):
        _seed_legacy()
        stamp = "2026-08-10T00:00:00Z"
        with database.SessionLocal() as db:
            db.add(
                database.StableAgentModel(
                    agent_id=AGENT_ID,
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
                        agent_id=AGENT_ID,
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
        outcome = _resolve()
        assert outcome["status"] == "roster-incarnation-ambiguous-or-invalid"
        assert outcome["agent"] is None

    def test_superseded_terminal_row_cannot_resolve(self, isolated_memory_db, harness):
        _seed_managed(lifecycle="superseded")
        _seed_roster()
        outcome = _resolve()
        assert outcome["status"] == "terminal-pane-mismatch-or-superseded"
        assert outcome["agent"] is None

    def test_resolution_is_byte_for_byte_read_only(self, isolated_memory_db, harness):
        _seed_managed()
        _seed_roster()
        before = _dump_all_rows()
        outcome = _resolve()
        after = _dump_all_rows()
        assert outcome["status"] == "resolved"
        assert after == before
        # The resolver observes the pane, the server, and the process start
        # marker — all read-only.
        assert harness.calls == ["pane-identity", "server-identity", "start-marker"]

    def test_caller_terminal_id_cannot_override_the_pane_mapping(self, isolated_memory_db, harness):
        # A different registered terminal on another pane must not be
        # reachable: the service only ever reads the mapping of the observed
        # pane.
        _seed_managed(
            terminal_id="other01",
            generation="00000000-0000-4000-8000-000000000002",
            pane_id="%9",
            window_id="@9",
            pane_pid=7777,
        )
        _seed_roster(
            terminal_id="other01",
            generation="00000000-0000-4000-8000-000000000002",
            agent_id=_uuid(),
            native_session_id="77777777-6666-4555-8444-333333333333",
            pane_id="%9",
            pane_pid=7777,
        )
        _seed_managed()  # the pane under observation
        _seed_roster()

        outcome = _resolve()
        assert outcome["status"] == "resolved"
        assert outcome["terminal"]["terminal_id"] == TERMINAL_ID
        assert outcome["terminal"]["terminal_id"] != "other01"


class TestRoundFourPaneGates:
    def test_superseded_pointer_on_live_row_refuses(self, isolated_memory_db, harness):
        """A live-lifecycle row that still carries a supersession pointer
        cannot resolve (the pointer is exposed truthfully for both vintages)."""
        _seed_managed()
        with database.SessionLocal() as db:
            row = db.query(database.TerminalModel).filter_by(id=TERMINAL_ID).one()
            row.lifecycle_state = "live"
            row.superseded_by_terminal_id = "other01"
            row.superseded_by_generation = "00000000-0000-4000-8000-000000000002"
            db.commit()
        _seed_roster()
        outcome = _resolve()
        assert outcome["status"] == "terminal-pane-mismatch-or-superseded"
        assert outcome["agent"] is None

    def test_pid_start_marker_drift_refuses(self, isolated_memory_db, harness, monkeypatch):
        """Same tmux tuple and pid but a changed live start marker means the
        process was recycled: typed stale/invalid non-identity."""
        _seed_managed()
        _seed_roster()
        harness.live_start_marker = "Fri Jul 25 09:00:00 2026"
        outcome = _resolve()
        assert outcome["status"] == "roster-incarnation-ambiguous-or-invalid"
        assert outcome["agent"] is None

        harness.live_start_marker = None
        outcome = _resolve()
        assert outcome["status"] == "roster-incarnation-ambiguous-or-invalid"

    def test_mismatched_current_lineage_pointer_refuses(self, isolated_memory_db, harness):
        """The incarnation's lineage and the agent's current lineage must
        agree; a drifted pointer is ambiguous/invalid."""
        _seed_managed()
        _seed_roster()
        with database.SessionLocal() as db:
            agent = db.query(database.StableAgentModel).filter_by(agent_id=AGENT_ID).one()
            agent.current_lineage_id = "00000000-0000-4000-8000-0000000000ff"
            db.commit()
        outcome = _resolve()
        assert outcome["status"] == "roster-incarnation-ambiguous-or-invalid"
        assert outcome["agent"] is None
