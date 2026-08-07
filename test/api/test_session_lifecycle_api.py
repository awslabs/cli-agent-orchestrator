"""HTTP surface for declared session state.

The load-bearing property here is one an ordinary route would get wrong by
inheritance: these routes must never consult tmux. `GET /sessions/{name}`
fails closed on `session_exists` first, which is right for a route
returning live terminals and fatal for one returning declared state — a
`stopped` session is *defined* by its tmux session being gone.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.services import session_lifecycle as sl

SESSION = "cao-p1-closure"


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/lifecycle-api.db")
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(sl.database, "SessionLocal", sessionmaker(bind=engine))
    return engine


class TestReads:
    def test_the_suppression_verdict_travels_with_the_record(self, client):
        """So the conductor never keeps a second copy of the suppressing set.

        A drifted copy over there fails toward a marshal that stays quiet,
        which is the failure nobody notices.
        """
        payload = client.get(f"/sessions/{SESSION}/lifecycle").json()
        assert payload["suppresses_marshal"] is False
        sl.declare(SESSION, sl.COMPLETE, declared_by="supervisor")
        assert client.get(f"/sessions/{SESSION}/lifecycle").json()["suppresses_marshal"] is True

    def test_a_pausing_session_reports_not_suppressed_and_its_overdue_flag(self, client):
        sl.request_pause(SESSION, requested_by="colin", deadline_seconds=1)
        payload = client.get(f"/sessions/{SESSION}/lifecycle").json()
        assert payload["lifecycle"] == "pausing"
        assert payload["suppresses_marshal"] is False
        assert "pause_overdue" in payload

    def test_an_undeclared_session_reads_as_working_rather_than_404(self, client):
        response = client.get(f"/sessions/{SESSION}/lifecycle")
        assert response.status_code == 200
        assert response.json()["lifecycle"] == "working"
        assert response.json()["declared"] is False

    def test_a_stopped_session_is_still_readable(self, client):
        """The route that would have inherited the tmux guard.

        A stopped session has no tmux session by definition. If this
        route failed closed on existence the way `GET /sessions/{name}`
        does, the state whose entire purpose is to outlive the panes would
        be unreadable.
        """
        sl.stop(SESSION, declared_by="colin")
        payload = client.get(f"/sessions/{SESSION}/lifecycle").json()
        assert payload["lifecycle"] == "stopped"
        assert payload["restore_to"] == "working"

    def test_listing_omits_archived_by_default(self, client):
        sl.set_archived(SESSION, True, declared_by="colin")
        assert client.get("/session-lifecycle").json()["count"] == 0
        assert (
            client.get("/session-lifecycle", params={"include_archived": "true"}).json()["count"]
            == 1
        )


class TestDeclare:
    def test_complete_is_a_declaration(self, client):
        response = client.post(
            f"/sessions/{SESSION}/lifecycle",
            json={"lifecycle": "complete", "declared_by": "supervisor-terra"},
        )
        assert response.status_code == 200
        assert response.json()["lifecycle"] == "complete"
        assert response.json()["declared_by"] == "supervisor-terra"

    def test_stopped_cannot_be_declared_here(self, client):
        """It must capture what a resume would restore to."""
        response = client.post(
            f"/sessions/{SESSION}/lifecycle",
            json={"lifecycle": "stopped", "declared_by": "colin"},
        )
        assert response.status_code == 422

    def test_an_unattributed_declaration_is_refused(self, client):
        response = client.post(f"/sessions/{SESSION}/lifecycle", json={"lifecycle": "complete"})
        assert response.status_code == 422

    def test_an_unknown_field_is_refused_rather_than_dropped(self, client):
        """A dropped `expected_epoch` silently becomes last-write-wins."""
        response = client.post(
            f"/sessions/{SESSION}/lifecycle",
            json={"lifecycle": "complete", "declared_by": "s", "expcted_epoch": 3},
        )
        assert response.status_code == 422

    def test_a_stale_epoch_is_a_conflict(self, client):
        first = sl.declare(SESSION, sl.WORKING, declared_by="a")
        sl.declare(SESSION, sl.COMPLETE, declared_by="b")
        response = client.post(
            f"/sessions/{SESSION}/lifecycle",
            json={
                "lifecycle": "working",
                "declared_by": "c",
                "expected_epoch": first["epoch"],
            },
        )
        assert response.status_code == 409


class TestPause:
    def test_a_request_enters_pausing_without_granting(self, client):
        payload = client.post(
            f"/sessions/{SESSION}/lifecycle/pause-request",
            json={"requested_by": "colin"},
        ).json()
        assert payload["lifecycle"] == "pausing"
        assert payload["pause_deadline_at"]

    def test_the_supervisor_settles_it(self, client):
        client.post(f"/sessions/{SESSION}/lifecycle/pause-request", json={"requested_by": "colin"})
        payload = client.post(
            f"/sessions/{SESSION}/lifecycle/pause-settled",
            json={"declared_by": "supervisor-terra"},
        ).json()
        assert payload["lifecycle"] == "paused"

    def test_settling_a_pause_nobody_requested_is_a_conflict(self, client):
        response = client.post(
            f"/sessions/{SESSION}/lifecycle/pause-settled", json={"declared_by": "s"}
        )
        assert response.status_code == 409

    def test_an_absurd_deadline_is_refused(self, client):
        response = client.post(
            f"/sessions/{SESSION}/lifecycle/pause-request",
            json={"requested_by": "colin", "deadline_seconds": 999999},
        )
        assert response.status_code == 422


class TestStopIsGatedOnAnAcknowledgement:
    """Stopping is currently irreversible for every worker.

    The spec's rule is that proceeding is allowed and proceeding
    *unknowingly* is not, so the route refuses a client that has not said
    it read the impact.
    """

    def test_stopping_without_acknowledging_is_refused(self, client):
        response = client.post(
            f"/sessions/{SESSION}/lifecycle/stop",
            json={"declared_by": "colin", "acknowledged_one_way": False},
        )
        assert response.status_code == 400
        assert "stop-impact" in response.json()["detail"]
        assert sl.describe(SESSION)["lifecycle"] == "working"

    def test_acknowledging_records_the_stop_and_its_restore_target(self, client):
        sl.declare(SESSION, sl.COMPLETE, declared_by="supervisor")
        payload = client.post(
            f"/sessions/{SESSION}/lifecycle/stop",
            json={"declared_by": "colin", "acknowledged_one_way": True},
        ).json()
        assert payload["lifecycle"] == "stopped"
        assert payload["restore_to"] == "complete"

    def test_the_acknowledgement_is_required_not_defaulted(self, client):
        response = client.post(f"/sessions/{SESSION}/lifecycle/stop", json={"declared_by": "colin"})
        assert response.status_code == 422


class TestStopImpactIsComputed:
    def _rows(self, monkeypatch, rows):
        from cli_agent_orchestrator.services import terminal_projection

        monkeypatch.setattr(terminal_projection, "project_session", lambda name: rows)

    def test_it_reports_that_nothing_can_be_resumed_today(self, client, monkeypatch):
        """The honest answer, and the one a provider table would not give."""
        self._rows(
            monkeypatch,
            [
                {"terminal_id": "a1", "provider": "claude_code", "lifecycle_state": "live"},
                {"terminal_id": "b2", "provider": "muse_cli", "lifecycle_state": "live"},
                {
                    "terminal_id": "c3",
                    "provider": "codex",
                    "native_session_id": "x",
                    "lifecycle_state": "live",
                },
            ],
        )
        payload = client.get(f"/sessions/{SESSION}/stop-impact").json()
        assert payload["live_workers"] == 3
        assert payload["one_way_for_every_worker"] is True
        assert payload["resume_machinery_available"] is False
        reasons = {v["provider"]: v["reason"] for v in payload["not_resumable"]}
        assert reasons["muse_cli"] == "provider-has-no-resume-path"
        assert reasons["claude_code"] == "no-recorded-native-session-id"
        assert reasons["codex"] == "provider-version-drifted-from-pin"

    def test_a_worker_whose_liveness_cannot_be_read_is_a_loss(self, client, monkeypatch):
        """ "Not live" and "we could not look" are different facts.

        A tmux server that cannot be read makes every row unobservable —
        and that is exactly when somebody reaches for stop. Answering
        "0 workers" there would be this module's own failure mode reached
        through its own filter.
        """
        self._rows(
            monkeypatch,
            [
                {
                    "terminal_id": "a1",
                    "provider": "codex",
                    "lifecycle_state": "unknown-liveness",
                    "lifecycle_reason": "tmux server could not be read",
                }
            ],
        )
        payload = client.get(f"/sessions/{SESSION}/stop-impact").json()
        assert payload["live_workers"] == 1
        assert payload["not_resumable"][0]["reason"] == "liveness-could-not-be-observed"
        assert "tmux server" in payload["not_resumable"][0]["detail"]

    def test_a_dead_pane_is_not_counted_as_a_loss(self, client, monkeypatch):
        """It is already gone; naming it would inflate what is being accepted."""
        self._rows(
            monkeypatch,
            [
                {"terminal_id": "a1", "provider": "codex", "lifecycle_state": "dead"},
                {"terminal_id": "b2", "provider": "codex", "lifecycle_state": "superseded"},
            ],
        )
        assert client.get(f"/sessions/{SESSION}/stop-impact").json()["live_workers"] == 0

    def test_the_impact_resolves_the_same_session_the_stop_records(self, client, monkeypatch):
        """The bare name reads zero workers; the write normalises and stops the real one.

        The dialog would say nothing will be lost, the operator would agree,
        and a live fleet would be silenced to the marshal on the strength
        of it.
        """
        seen: list[str] = []

        from cli_agent_orchestrator.services import terminal_projection

        def _record(name):
            seen.append(name)
            return [{"terminal_id": "a1", "provider": "codex", "lifecycle_state": "live"}]

        monkeypatch.setattr(terminal_projection, "project_session", _record)
        payload = client.get("/sessions/p1-closure/stop-impact").json()
        assert seen == [SESSION], "the read resolved a different session than the write would"
        assert payload["session_name"] == SESSION
        assert payload["live_workers"] == 1


class TestKindAndArchive:
    def test_kind_is_declared(self, client):
        payload = client.post(
            f"/sessions/{SESSION}/lifecycle/kind",
            json={"kind": "service", "declared_by": "colin"},
        ).json()
        assert payload["kind"] == "service"

    def test_an_unknown_kind_is_refused(self, client):
        response = client.post(
            f"/sessions/{SESSION}/lifecycle/kind",
            json={"kind": "daemon", "declared_by": "colin"},
        )
        assert response.status_code == 422

    def test_archiving_forces_a_stop_and_remembers_the_state(self, client):
        sl.declare(SESSION, sl.COMPLETE, declared_by="supervisor")
        payload = client.post(
            f"/sessions/{SESSION}/lifecycle/archived",
            json={"archived": True, "declared_by": "colin", "acknowledged_one_way": True},
        ).json()
        assert payload["archived"] is True
        assert payload["restore_to"] == "complete"

    def test_archiving_carries_a_stops_obligations(self, client):
        """It reaches the same state as /stop and must not be the cheap door."""
        response = client.post(
            f"/sessions/{SESSION}/lifecycle/archived",
            json={"archived": True, "declared_by": "colin"},
        )
        assert response.status_code == 400
        assert "stop-impact" in response.json()["detail"]
        assert sl.describe(SESSION)["lifecycle"] == "working"

    def test_unarchiving_needs_no_acknowledgement(self, client):
        """Clearing a flag collects nothing."""
        sl.set_archived(SESSION, True, declared_by="colin")
        response = client.post(
            f"/sessions/{SESSION}/lifecycle/archived",
            json={"archived": False, "declared_by": "colin"},
        )
        assert response.status_code == 200
        assert response.json()["archived"] is False


class TestScopes:
    def test_stop_demands_admin_and_reads_accept_read(self):
        """Collecting a fleet irreversibly is not a write-scope operation."""
        from cli_agent_orchestrator.api import session_lifecycle as api
        from cli_agent_orchestrator.security.auth import SCOPE_ADMIN, SCOPE_READ

        def _required(dependency) -> tuple:
            return next(
                cell.cell_contents
                for cell in dependency.call.__closure__
                if isinstance(cell.cell_contents, tuple)
            )

        by_path = {}
        for route in api.router.routes:
            by_path[(route.path, tuple(sorted(route.methods)))] = _required(
                route.dependant.dependencies[0]
            )

        assert by_path[("/sessions/{session_name}/lifecycle/stop", ("POST",))] == (SCOPE_ADMIN,)
        assert SCOPE_READ in by_path[("/sessions/{session_name}/lifecycle", ("GET",))]
