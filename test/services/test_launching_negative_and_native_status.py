"""Finalizing a stuck ``launching`` generation, and telling the truth about it.

Two live generations sat in ``launching`` with no binding: permanently
unfinalizable, because only ``preflight_blocked`` and ``bound`` could
finalize, *and* unreplaceable, because a generation is non-reusable once
issued. Preservation and finalizability were opposites, and the generation
was left in a state nothing could close.

They also reported ``status: unknown`` through the ordinary API for as long
as they lived, because the status came from a FIFO monitor that a native
TUI has no stream for.
"""

from __future__ import annotations

import hashlib
import subprocess
import uuid

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.managed_launch_v2 import (
    PROTOCOL_VERSION_V2,
    ManagedLaunchV2NegativeRequest,
    ManagedLaunchV2ReserveRequest,
)
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import execution_mode as em
from cli_agent_orchestrator.services import managed_launch_v2 as v2
from cli_agent_orchestrator.services import terminal_projection
from cli_agent_orchestrator.services.managed_launch import ManagedLaunchConflict

K27 = "kimi-code/kimi-for-coding"


@pytest.fixture
def _companion(tmp_path, monkeypatch):
    monkeypatch.setattr(v2, "COMPANION_DIR", tmp_path / "companion")


@pytest.fixture
def worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _reserve(worktree, tmp_path, *, provider="kimi_cli", model=K27) -> dict:
    executable = tmp_path / f"fake-{provider}"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    record, _created = v2.reserve(
        ManagedLaunchV2ReserveRequest(
            protocol_version=PROTOCOL_VERSION_V2,
            reservation_id=str(uuid.uuid4()),
            session_name="cao-chess-shakedown",
            provider=provider,
            agent_profile="reviewer",
            caller_id="deadbeef",
            working_directory=str(worktree),
            expected_model=model,
            expected_effort="provider-default",
            provider_executable=str(executable),
            provider_executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
            obligation_generation="obgen-7c2e4a1b",
            task_id="self-heal-demo-task",
            run_id="run-0001",
            delivery_id=str(uuid.uuid4()),
            launch_nonce="n" * 40,
            execution_mode="native_tui",
            worker_class="persistent",
        )
    )
    return record


def _set(reservation_id: str, **columns) -> None:
    with database.SessionLocal() as db:
        db.query(database.ManagedLaunchV2ReservationModel).filter(
            database.ManagedLaunchV2ReservationModel.reservation_id == reservation_id
        ).update(columns, synchronize_session=False)
        db.commit()


def _negative_request(record) -> ManagedLaunchV2NegativeRequest:
    return ManagedLaunchV2NegativeRequest(
        protocol_version=PROTOCOL_VERSION_V2,
        finalize_id=str(uuid.uuid4()),
        terminal_id=record["terminal_id"],
        generation=record["generation"],
        obligation_generation=record["obligation_generation"],
        reason="launch never reached its bind",
    )


class TestFinalizingAStuckLaunchingGeneration:
    def test_a_launching_row_with_nothing_published_finalizes(
        self, isolated_memory_db, _companion, worktree, tmp_path
    ):
        """The reproduced state: launching, no binding, no intent, no admission.

        Nothing could have delivered a task byte, because admission
        requires a bind and no bind was ever published -- so the negative
        states a fact rather than assuming one.
        """
        record = _reserve(worktree, tmp_path)
        _set(record["reservation_id"], state="launching")

        result = v2.finalize_negative(record["reservation_id"], _negative_request(record))

        assert result["state"] == "negative"

    def test_the_finalization_names_the_state_it_came_from(
        self, isolated_memory_db, _companion, worktree, tmp_path
    ):
        record = _reserve(worktree, tmp_path)
        _set(record["reservation_id"], state="launching")

        result = v2.finalize_negative(record["reservation_id"], _negative_request(record))

        assert result["admission"]["finalized_from_state"] == "launching"
        assert result["admission"]["task_bytes_submitted"] is False

    def test_the_observed_pane_identity_is_carried_into_the_negative(
        self, isolated_memory_db, _companion, worktree, tmp_path
    ):
        """So a live pane stays reconcilable under its exact generation.

        Without it the call that closes the reservation orphans the pane it
        was observed on -- preservation and finalizability become opposites
        again, one step further along.
        """
        record = _reserve(worktree, tmp_path)
        database.create_terminal_v2(
            terminal_id=record["terminal_id"],
            tmux_session="cao-chess-shakedown",
            tmux_window="w",
            provider="kimi_cli",
            generation=record["generation"],
            pane_id="%30",
            window_id="@30",
            server_socket_path="/private/tmp/tmux-501/default",
            session_id="$7",
            pane_pid=54321,
        )
        _set(record["reservation_id"], state="launching")

        result = v2.finalize_negative(record["reservation_id"], _negative_request(record))

        assert result["admission"]["observed_pane_identity"] == {
            "server_socket_path": "/private/tmp/tmux-501/default",
            "session_id": "$7",
            "window_id": "@30",
            "pane_id": "%30",
            "pane_pid": 54321,
        }

    def test_an_unobserved_pane_is_null_rather_than_partial(
        self, isolated_memory_db, _companion, worktree, tmp_path
    ):
        """Absent is a different claim from partial, and only the row can say."""
        record = _reserve(worktree, tmp_path)
        database.create_terminal_v2(
            terminal_id=record["terminal_id"],
            tmux_session="cao-chess-shakedown",
            tmux_window="w",
            provider="kimi_cli",
            generation=record["generation"],
            pane_id="%30",
            window_id="@30",
        )
        _set(record["reservation_id"], state="launching")

        result = v2.finalize_negative(record["reservation_id"], _negative_request(record))

        assert result["admission"]["observed_pane_identity"] is None


class TestTheOtherDirection:
    """Any one of the three present means it cannot be proven."""

    @pytest.mark.parametrize(
        "column",
        ["binding_json", "bind_intent_json", "admission_json"],
    )
    def test_a_launching_row_that_reached_the_write_path_refuses(
        self, isolated_memory_db, _companion, worktree, tmp_path, column
    ):
        record = _reserve(worktree, tmp_path)
        _set(record["reservation_id"], state="launching", **{column: '{"any": "value"}'})

        with pytest.raises(ManagedLaunchConflict) as raised:
            v2.finalize_negative(record["reservation_id"], _negative_request(record))

        assert column in str(raised.value)

    def test_a_bind_intent_alone_is_enough_to_refuse(
        self, isolated_memory_db, _companion, worktree, tmp_path
    ):
        """It is journaled before the binding is published.

        So its presence means a bind was in flight and may still land --
        finalizing over it is the race the compare-and-set exists to lose.
        """
        record = _reserve(worktree, tmp_path)
        _set(record["reservation_id"], state="launching", bind_intent_json='{"attempt": "1"}')

        with pytest.raises(ManagedLaunchConflict):
            v2.finalize_negative(record["reservation_id"], _negative_request(record))

        with database.SessionLocal() as db:
            assert v2._query(db, record["reservation_id"]).state == "launching"


class TestTheRaceResolvesOneWayOnly:
    def test_a_bind_landing_first_wins_and_the_finalization_loses(
        self, isolated_memory_db, _companion, worktree, tmp_path, monkeypatch
    ):
        """The compare-and-set re-states what the read proved.

        A bind that writes its binding between the read and the write flips
        one of the three columns, so the CAS matches nothing and the
        finalization fails rather than closing a generation that just bound.
        """
        record = _reserve(worktree, tmp_path)
        _set(record["reservation_id"], state="launching")

        original = v2._observed_pane_identity

        def _bind_lands(db, row):
            # Runs after the read and before the update: exactly the window
            # the CAS exists to cover.
            _set(record["reservation_id"], binding_json='{"bound": true}')
            return original(db, row)

        monkeypatch.setattr(v2, "_observed_pane_identity", _bind_lands)

        with pytest.raises(ManagedLaunchConflict, match="CAS"):
            v2.finalize_negative(record["reservation_id"], _negative_request(record))

        with database.SessionLocal() as db:
            assert v2._query(db, record["reservation_id"]).state == "launching"

    def test_a_bind_arriving_after_the_negative_finds_it_negative(
        self, isolated_memory_db, _companion, worktree, tmp_path
    ):
        record = _reserve(worktree, tmp_path)
        _set(record["reservation_id"], state="launching")

        v2.finalize_negative(record["reservation_id"], _negative_request(record))

        with database.SessionLocal() as db:
            assert v2._query(db, record["reservation_id"]).state == "negative"

    def test_finalizing_twice_converges_instead_of_double_writing(
        self, isolated_memory_db, _companion, worktree, tmp_path
    ):
        record = _reserve(worktree, tmp_path)
        _set(record["reservation_id"], state="launching")

        first = v2.finalize_negative(record["reservation_id"], _negative_request(record))
        second = v2.finalize_negative(record["reservation_id"], _negative_request(record))

        assert first["admission"] == second["admission"]


class TestTheOrdinaryApiTellsTheTruthAboutANativeTerminal:
    """``unknown`` promised a detection that was never coming.

    It means "live pane, state not yet detected" -- for a native TUI no
    detection exists at all, so every native worker looked pending forever.
    """

    def _live_native(self, record, monkeypatch):
        database.create_terminal_v2(
            terminal_id=record["terminal_id"],
            tmux_session="cao-chess-shakedown",
            tmux_window="w",
            provider="kimi_cli",
            generation=record["generation"],
            pane_id="%30",
            window_id="@30",
            server_socket_path="/private/tmp/tmux-501/default",
            session_id="$7",
            pane_pid=54321,
        )
        monkeypatch.setattr(
            terminal_projection,
            "observed_lifecycle",
            lambda row, panes: (terminal_projection.LIFECYCLE_LIVE, None),
        )

    def test_a_live_native_terminal_reports_not_fifo_monitored(
        self, isolated_memory_db, _companion, worktree, tmp_path, monkeypatch
    ):
        record = _reserve(worktree, tmp_path)
        self._live_native(record, monkeypatch)

        projected = terminal_projection.project_terminal(record["terminal_id"])

        assert projected["status"] == TerminalStatus.NOT_FIFO_MONITORED.value
        assert projected["status"] != "unknown"

    def test_it_states_the_absence_rather_than_leaving_it_inferred(
        self, isolated_memory_db, _companion, worktree, tmp_path, monkeypatch
    ):
        record = _reserve(worktree, tmp_path)
        self._live_native(record, monkeypatch)

        assert (
            terminal_projection.project_terminal(record["terminal_id"])["fifo_monitored"] is False
        )

    def test_identity_and_lifecycle_are_still_fully_reported(
        self, isolated_memory_db, _companion, worktree, tmp_path, monkeypatch
    ):
        """Only the classification is missing; everything observed is reported.

        Absence is not truthful either -- the dashboard must show a live
        native generation's real identity, not hide it.
        """
        record = _reserve(worktree, tmp_path)
        self._live_native(record, monkeypatch)

        projected = terminal_projection.project_terminal(record["terminal_id"])

        assert projected["protocol_vintage"] == "v2"
        assert projected["pane_id"] == "%30"
        assert projected["window_id"] == "@30"
        assert projected["pane_pid"] == 54321
        assert projected["generation"] == record["generation"]
        assert projected["lifecycle_state"] == terminal_projection.LIFECYCLE_LIVE

    def test_an_acp_v2_terminal_is_still_fifo_monitored(
        self, isolated_memory_db, _companion, worktree, tmp_path, monkeypatch
    ):
        """The mode decides it, not the vintage and not the argv.

        The ACP bridge is a v2 terminal launched by argv that does have a
        FIFO, so anything inferring nativeness from either would call it
        native and stop reporting its status.
        """
        record = _reserve(worktree, tmp_path)
        _set(record["reservation_id"], execution_mode=em.ACP)
        self._live_native(record, monkeypatch)
        monkeypatch.setattr(terminal_projection, "_provider_status", lambda _id: "idle")

        projected = terminal_projection.project_terminal(record["terminal_id"])

        assert projected["fifo_monitored"] is True
        assert projected["status"] == "idle"

    def test_the_new_field_is_part_of_the_agreed_projection(self):
        assert "fifo_monitored" in terminal_projection.PROJECTION_FIELDS


class TestTheFifoMonitorIsNotScheduledForANativeTui:
    """Decided where the monitor is scheduled, never by catching its raise.

    A native TUI owns its pane's argv and renders a full-screen interface,
    so there is no line-oriented stream for a FIFO to carry and no legacy
    provider to parse one. Left scheduled it did not degrade quietly: every
    output chunk reached a provider lookup that raises for a terminal the
    legacy table does not hold, which is the observed log storm.

    The backend here reports no event inbox, so the FIFO branch is actually
    reachable -- otherwise these would pass against any implementation.
    """

    def _harness(self, tmp_path, monkeypatch):
        import asyncio

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from cli_agent_orchestrator import constants
        from cli_agent_orchestrator.services import terminal_service as terminals

        engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}")
        database.Base.metadata.create_all(engine)
        monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
        for name, path in (
            ("CAO_HOME_DIR", tmp_path / "home"),
            ("COMPANION_DIR", tmp_path / "companion"),
        ):
            path.mkdir(exist_ok=True)
            monkeypatch.setattr(constants, name, path)
        for name in ("FIFO_DIR", "TERMINAL_LOG_DIR"):
            path = tmp_path / name.lower()
            path.mkdir(exist_ok=True)
            monkeypatch.setattr(terminals, name, path)
        monkeypatch.setattr(terminals, "_verify_managed_pane_process", lambda *a: None)
        monkeypatch.setattr(terminals, "dispatch_plugin_event", lambda *a, **k: None)
        monkeypatch.setattr(terminals, "get_herdr_inbox_service", lambda: None)
        monkeypatch.setattr(
            terminals,
            "load_agent_profile",
            lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("missing profile")),
        )

        class _Backend:
            def session_exists(self, _session):
                return True

            def create_window_with_argv(self, _s, window, _t, _a, _c, extra_env=None):
                return window

            def create_window(self, _s, window, *a, **k):
                # The v1 path, which builds a shell window rather than
                # handing the pane its own argv.
                return window

            def window_identity(self, _session, _window):
                # Unique per window: the resource registry is a process
                # singleton with a UNIQUE constraint on the observed tmux
                # id, so a shared literal would collide across launches
                # rather than exercise them.
                suffix = abs(hash(_window)) % 100000
                return {
                    "pane_id": f"%{suffix}",
                    "window_id": f"@{suffix}",
                    "session_id": "$9",
                    "server_socket_path": "/private/tmp/tmux-501/default",
                    "pane_pid": 9012,
                }

            def window_exists(self, _session, _window):
                return False

            def supports_event_inbox(self):
                # The pipe-pane path, so the FIFO branch is genuinely live.
                return False

            def pipe_pane(self, *_a):
                return None

            def send_special_key(self, *_a, **_k):
                # The v1 path types an Enter into its shell; the managed
                # native path deliberately does not.
                return None

        monkeypatch.setattr(terminals, "get_backend", lambda: _Backend())

        scheduled: list[str] = []
        monkeypatch.setattr(
            terminals.fifo_manager,
            "create_reader",
            lambda tid, **kwargs: scheduled.append(tid),
        )

        def _launch(**overrides):
            kwargs = {
                "provider": "codex",
                "agent_profile": "missing-profile",
                "session_name": "cao-fifo-scope",
                "working_directory": str(tmp_path),
                "reserved_terminal_id": overrides.pop("terminal_id", "d1e2f3a4"),
                "terminal_generation": str(uuid.uuid4()),
                "managed_native_command": ["/bin/true"],
                "protocol_vintage": "v2",
            }
            kwargs.update(overrides)
            return asyncio.run(terminals.create_terminal(**kwargs))

        return _launch, scheduled

    def test_a_native_tui_schedules_no_fifo_reader(self, isolated_memory_db, tmp_path, monkeypatch):
        launch, scheduled = self._harness(tmp_path, monkeypatch)

        launch(native_status_source=True)

        assert scheduled == []

    def test_the_acp_bridge_still_schedules_one(self, isolated_memory_db, tmp_path, monkeypatch):
        """The half that must NOT change.

        The v2 ACP bridge is also launched by argv and is a line-oriented
        subprocess that does need the FIFO -- which is why the mode is
        stated by the caller rather than inferred from the argv or vintage.
        """
        launch, scheduled = self._harness(tmp_path, monkeypatch)

        launch(terminal_id="acc12345")

        assert scheduled == ["acc12345"]

    def test_the_skip_is_decided_by_mode_not_by_vintage(
        self, isolated_memory_db, tmp_path, monkeypatch
    ):
        """Both launches above are v2, and they differ only in the mode.

        That is the whole point of stating it at the call: vintage cannot
        decide this, because both the native TUI and the ACP bridge are v2.
        A v1 launch is not driven here -- it needs a real shell to
        initialize -- but its scheduling is unchanged by construction, and
        the terminal-service suites report a failure set byte-identical to
        the untouched base.
        """
        launch, scheduled = self._harness(tmp_path, monkeypatch)

        launch(terminal_id="bb112233", native_status_source=True)
        launch(terminal_id="cc112233")

        assert scheduled == ["cc112233"]
