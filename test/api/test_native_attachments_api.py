"""HTTP surface for provider-native session claims.

Two things here are more than plumbing. The adjudication route takes its
own survivor observation server-side rather than accepting one from the
caller — otherwise the one party with a motive to release a session also
supplies the evidence that doing so is safe. And the routes are mounted
under `/native-attachments` because `/terminals/{id}/attachments` already
exists and means *image* attachments on operator messages.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.services import native_attachment as na

PROVIDER = "kimi_cli"
SESSION = "session_326c5026"


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/attachment-api.db")
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(na.database, "SessionLocal", sessionmaker(bind=engine))
    return engine


def _reaped_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


def _intent() -> dict:
    return na.acquire_intent(
        acquisition_method=na.ACQUISITION_ACP_BOOTSTRAP,
        acquisition_receipt={"kind": "kimi-acp-session-new", "session_id": SESSION},
        admits_only_new_instructions=True,
        replays_task_bytes=False,
        bootstrap_sent_no_turn=True,
        bootstrap_detached_before_launch=True,
    )


def _attach(*, pid: int) -> dict:
    owner = {
        "terminal_id": "44dda40b",
        "generation": "6e3d642e",
        "execution_mode": "native_tui",
    }
    na.declare(
        provider=PROVIDER, native_session_id=SESSION, pane_id="%12", intent=_intent(), **owner
    )
    na.mark_starting(provider=PROVIDER, native_session_id=SESSION, **owner)
    return na.mark_attached(
        provider=PROVIDER,
        native_session_id=SESSION,
        process_identity=na.process_identity(pid=pid, start_marker="Fri Aug  7 12:51:19 2026"),
        **owner,
    )


def _freeze() -> dict:
    na.declare(
        provider=PROVIDER,
        native_session_id=SESSION,
        terminal_id="44dda40b",
        generation="6e3d642e",
        execution_mode="native_tui",
        intent=_intent(),
    )
    return na.mark_ambiguous(
        provider=PROVIDER, native_session_id=SESSION, reason="pane_create_outcome_unknown"
    )


def _body(**overrides) -> dict:
    payload = {
        "outcome": "owner-proven-gone",
        "evidence_sha256": "d" * 64,
        "detail": "pane and process both gone for six days",
        "operator": "colin",
    }
    payload.update(overrides)
    return payload


class TestReads:
    def test_listing_returns_the_held_claims(self, client):
        _attach(pid=_reaped_pid())
        response = client.get("/native-attachments")
        assert response.status_code == 200
        assert response.json()["count"] == 1

    def test_a_single_claim_carries_a_fresh_observation(self, client):
        """The two fields an adjudicating human needs, and a live look."""
        _attach(pid=_reaped_pid())
        payload = client.get(f"/native-attachments/{PROVIDER}/{SESSION}").json()
        assert payload["owner"]["process_identity"]["pid"]
        assert payload["owner"]["pane_id"] == "%12"
        assert payload["observation"]["disposition"] == "gone"

    def test_the_observation_can_be_skipped(self, client):
        _attach(pid=_reaped_pid())
        payload = client.get(
            f"/native-attachments/{PROVIDER}/{SESSION}", params={"observe": "false"}
        ).json()
        assert "observation" not in payload

    def test_an_unclaimed_session_is_a_404(self, client):
        assert client.get(f"/native-attachments/{PROVIDER}/nope").status_code == 404

    def test_the_sweep_route_does_not_shadow_a_provider_named_sweep(self, client):
        """Route arity keeps these apart; this is the pin that it stays so."""
        assert client.post("/native-attachments/sweep", json={"apply": False}).status_code == 200


class TestSweepRoute:
    def test_the_sweep_defaults_to_reporting(self, client):
        _attach(pid=_reaped_pid())
        report = client.post("/native-attachments/sweep", json={}).json()
        assert report["applied"] is False
        assert na.get(PROVIDER, SESSION)["state"] == na.ATTACHED

    def test_apply_releases_the_provably_gone(self, client):
        _attach(pid=_reaped_pid())
        report = client.post("/native-attachments/sweep", json={"apply": True}).json()
        assert report["counts"]["released"] == 1
        assert na.get(PROVIDER, SESSION)["state"] == na.DETACHED

    def test_an_unknown_field_is_refused_rather_than_dropped(self, client):
        response = client.post("/native-attachments/sweep", json={"aply": True})
        assert response.status_code == 422


class TestAdjudicationRoute:
    def test_adjudicating_a_frozen_claim_releases_it(self, client):
        _freeze()
        response = client.post(f"/native-attachments/{PROVIDER}/{SESSION}/adjudicate", json=_body())
        assert response.status_code == 200
        assert response.json()["state"] == na.DETACHED

    def test_a_live_owner_is_a_conflict_not_a_bad_request(self, client):
        """409 is the only status that says "correct request, refused anyway"."""
        _attach(pid=os.getpid())
        na.mark_ambiguous(provider=PROVIDER, native_session_id=SESSION, reason="render mismatch")
        response = client.post(f"/native-attachments/{PROVIDER}/{SESSION}/adjudicate", json=_body())
        assert response.status_code == 409
        assert na.get(PROVIDER, SESSION)["state"] == na.AMBIGUOUS

    def test_the_caller_cannot_supply_its_own_survivor_observation(self, client):
        """The evidence that releasing is safe is not the requester's to write."""
        _freeze()
        response = client.post(
            f"/native-attachments/{PROVIDER}/{SESSION}/adjudicate",
            json=_body(live_survivors=[]),
        )
        assert response.status_code == 422

    def test_a_live_attachment_is_not_adjudicable(self, client):
        _attach(pid=_reaped_pid())
        response = client.post(f"/native-attachments/{PROVIDER}/{SESSION}/adjudicate", json=_body())
        assert response.status_code == 409

    @pytest.mark.parametrize(
        "overrides",
        [
            {"outcome": "probably-fine"},
            {"evidence_sha256": "short"},
            {"detail": ""},
            {"operator": ""},
        ],
    )
    def test_an_unattributable_adjudication_is_refused(self, client, overrides):
        _freeze()
        response = client.post(
            f"/native-attachments/{PROVIDER}/{SESSION}/adjudicate", json=_body(**overrides)
        )
        assert response.status_code == 422
        assert na.get(PROVIDER, SESSION)["state"] == na.AMBIGUOUS

    def test_an_unknown_session_is_a_404(self, client):
        response = client.post("/native-attachments/kimi_cli/nope/adjudicate", json=_body())
        assert response.status_code == 404


class TestScopes:
    def test_mutating_routes_demand_admin(self):
        """Deciding who may drive a provider session next is an admin call.

        The callback-recovery resolution route made the same judgement for
        the other ambiguous state in this system.
        """
        from cli_agent_orchestrator.api import attachments
        from cli_agent_orchestrator.security.auth import SCOPE_ADMIN, SCOPE_READ

        def _required(dependency) -> tuple:
            # `require_any_scope` closes over its arguments; the closure cell
            # is the only place the requirement survives to be asserted on.
            return next(
                cell.cell_contents
                for cell in dependency.call.__closure__
                if isinstance(cell.cell_contents, tuple)
            )

        checked = 0
        for route in attachments.router.routes:
            scopes = _required(route.dependant.dependencies[0])
            if route.methods == {"POST"}:
                assert scopes == (SCOPE_ADMIN,), route.path
            else:
                assert SCOPE_READ in scopes, route.path
            checked += 1
        assert checked == 5


class TestFreezeRoute:
    def test_an_unobservable_owner_can_be_frozen_for_adjudication(self, client, monkeypatch):
        from cli_agent_orchestrator.services import native_attachment_recovery as recovery

        _attach(pid=os.getpid())
        monkeypatch.setattr(recovery, "_pid_state", lambda pid: recovery.OWNER_UNOBSERVABLE)
        response = client.post(
            f"/native-attachments/{PROVIDER}/{SESSION}/freeze",
            json={"operator": "colin", "detail": "process table will not answer"},
        )
        assert response.status_code == 200
        assert response.json()["state"] == na.AMBIGUOUS

    def test_a_running_owner_cannot_be_frozen(self, client):
        _attach(pid=os.getpid())
        response = client.post(
            f"/native-attachments/{PROVIDER}/{SESSION}/freeze",
            json={"operator": "colin", "detail": "I want it back"},
        )
        assert response.status_code == 409
        assert na.get(PROVIDER, SESSION)["state"] == na.ATTACHED

    def test_a_provably_gone_owner_is_pointed_at_the_sweep(self, client):
        _attach(pid=_reaped_pid())
        response = client.post(
            f"/native-attachments/{PROVIDER}/{SESSION}/freeze",
            json={"operator": "colin", "detail": "looks dead"},
        )
        assert response.status_code == 409
        assert "sweep" in response.json()["detail"]

    def test_an_unattributable_freeze_is_refused(self, client):
        _attach(pid=os.getpid())
        response = client.post(
            f"/native-attachments/{PROVIDER}/{SESSION}/freeze", json={"detail": "no name"}
        )
        assert response.status_code == 422


class TestTheBootSweepIsWired:
    """Nothing asserted the lifespan calls it, so a silent removal was free."""

    def test_the_lifespan_runs_the_startup_sweep(self):
        import inspect

        from cli_agent_orchestrator.api import main

        source = inspect.getsource(main.lifespan)
        assert "native_attachment_recovery.sweep_at_startup" in source

    def test_the_startup_sweep_reports_rather_than_mutating(self, monkeypatch):
        """Two tests in this repository enter the real lifespan without
        stubbing the recovery steps. An applying default would release rows
        out of an operator's database as a side effect of running pytest."""
        from cli_agent_orchestrator.services import native_attachment_recovery as recovery

        monkeypatch.delenv(recovery.SWEEP_ON_BOOT_ENV, raising=False)
        _attach(pid=_reaped_pid())
        assert recovery.sweep_at_startup()["applied"] is False
        assert na.get(PROVIDER, SESSION)["state"] == na.ATTACHED
