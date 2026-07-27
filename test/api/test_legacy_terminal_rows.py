"""Legacy terminal rows through the real retrieval path (cond-0173, B4).

A historical row whose stored lifecycle is ``unknown-liveness`` — the
demotion writer's record of "this identity could not be observed", written
for every three-of-five row the previously deployed build left behind —
projected truthfully but then failed the ``Terminal`` response model's enum
validation. The ``ValidationError`` is a ``ValueError``, so the router's
not-found handler answered ``GET /terminals/{id}`` with 404 for a row that
exists: the row was effectively hidden, appearing absent to the operator
and to the recovery archive that has to enumerate it — and 404 is the very
signal the archive treats as a pane-absence proof.

These tests run the whole supported path — database, projection, response
model, and router — against a real database with only the tmux backend
faked, so the fixture is the row and the code under test is everything
else. They hold three contracts:

* the legacy row renders honestly (terminal/historical, never promoted to
  live or to a provider status it does not have) and is enumerable by the
  same listing the archive reads;
* current rows are byte-for-byte unaffected — a live pane still reports
  its provider status;
* the supported DELETE endpoint remains the row's only deletion path, and
  nothing about that path changed.
"""

from __future__ import annotations

from typing import Dict, Optional

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import terminal_projection as projection
from cli_agent_orchestrator.services import terminal_service

SOCKET = "/private/tmp/cao-legacy.sock"

LEGACY_ID = "aaaa0173"
DEAD_ID = "bbbb0173"
LIVE_ID = "cccc0173"

#: The provider answers a card may show for a live pane. A legacy row must
#: never wear one of these — "alive, not yet detected" is the phantom-card
#: failure this whole path exists to prevent.
PROVIDER_STATUSES = {
    "unknown",
    "idle",
    "processing",
    "completed",
    "waiting_user_answer",
    "error",
    "not_fifo_monitored",
}


class FakeBackend:
    """Answers pane enumeration and absorbs window teardown.

    The pane set is fixed at install: an empty dict is a server that
    answered and knows no panes, which is what a long-gone historical row
    looks like to the projection.
    """

    supports_pane_identity = True

    def __init__(self, panes: Optional[Dict[str, Dict[str, str]]]):
        self._panes = panes
        self.killed = []

    def observe_pane_identities(self):
        return self._panes

    def observe_pane_identity(self, pane_id):
        record = (self._panes or {}).get(pane_id)
        if record is None:
            return {"outcome": "absent"}
        return record

    # The teardown surface the supported DELETE path touches.
    def get_history(self, *_args, **_kwargs):
        return ""

    def get_pane_working_directory(self, *_args, **_kwargs):
        return "/tmp"

    def stop_pipe_pane(self, *_args, **_kwargs):
        return None

    def kill_window(self, session, window):
        self.killed.append((session, window))

    def window_exists(self, *_args, **_kwargs):
        return False


def _pane_record() -> Dict[str, str]:
    return {
        "outcome": "observed",
        "pane_id": "%10",
        "window_id": "@10",
        "session_id": "$1",
        "pane_pid": "4242",
        "session_name": "cao-legacy",
        "window_name": "worker",
        "dead": "0",
        "server_socket_path": SOCKET,
    }


def _create_legacy_row(terminal_id: str) -> None:
    """Write the row the way history actually wrote it.

    The previously deployed build recorded three of the five identity
    fields; the first read that could not complete the tuple demoted the
    row through the real writer, storing the lifecycle the enum boundary
    later choked on. The pane is absent from the backend's enumeration, so
    the projection's upgrade attempt lawfully completes nothing.
    """
    database.create_terminal(
        terminal_id=terminal_id,
        tmux_session="cao-legacy",
        tmux_window="worker",
        provider="claude_code",
        pane_id="%66",
        window_id="@66",
        server_socket_path=SOCKET,
    )
    database.record_terminal_lifecycle(
        terminal_id,
        state="unknown-liveness",
        reason="identity incomplete: missing session_id, pane_pid",
    )


@pytest.fixture
def backend(monkeypatch):
    def _install(panes):
        fake = FakeBackend(panes)
        monkeypatch.setattr("cli_agent_orchestrator.backends.registry._backend", fake)
        return fake

    return _install


class TestLegacyUnknownLivenessRow:
    def test_the_stored_row_renders_through_the_retrieval_path(
        self, client, isolated_memory_db, backend
    ):
        """The defect in one test: this answered 404 before the fix.

        The response model's ``ValidationError`` is a ``ValueError``, so the
        router's not-found handler reported the existing row as absent —
        the same 404 the archive relies on as a pane-absence proof.
        """
        backend({})
        _create_legacy_row(LEGACY_ID)

        response = client.get(f"/terminals/{LEGACY_ID}")

        assert response.status_code == 200
        body = response.json()
        assert body["id"] == LEGACY_ID
        assert body["lifecycle_state"] == "unknown-liveness"
        assert body["status"] == "unknown-liveness"
        assert "identity incomplete" in body["lifecycle_reason"]
        assert body["protocol_vintage"] == "v1"
        # Honest means historical, not adoptable: the row wears no provider
        # status and no live lifecycle, and its identity stays unrecorded
        # rather than completed by invention.
        assert body["status"] not in PROVIDER_STATUSES
        assert body["lifecycle_state"] not in projection.ATTACHABLE_LIFECYCLE_STATES

    def test_the_projection_does_not_rewrite_the_stored_row(
        self, client, isolated_memory_db, backend
    ):
        """Renderable, not repaired: no schema rewrite of stored rows."""
        backend({})
        _create_legacy_row(LEGACY_ID)

        client.get(f"/terminals/{LEGACY_ID}")

        stored = database.get_terminal_metadata(LEGACY_ID)
        assert stored["lifecycle_state"] == "unknown-liveness"
        assert stored["lifecycle_reason"] == "identity incomplete: missing session_id, pane_pid"
        assert stored["session_id"] is None
        assert stored["pane_pid"] is None

    def test_the_row_is_enumerable_through_the_session_listing(
        self, client, isolated_memory_db, backend
    ):
        """The archive enumerates rows through the listing; the row is in it."""
        backend({})
        _create_legacy_row(LEGACY_ID)

        response = client.get("/sessions/cao-legacy/terminals")

        assert response.status_code == 200
        rows = {row["terminal_id"]: row for row in response.json()}
        assert rows[LEGACY_ID]["lifecycle_state"] == "unknown-liveness"
        assert rows[LEGACY_ID]["status"] == "unknown-liveness"

    def test_a_provably_absent_historical_row_renders_dead(
        self, client, isolated_memory_db, backend
    ):
        """The same boundary hid the sibling lifecycle states too.

        A row with a complete recorded identity whose pane is gone is dead,
        and says so — the 404 it used to answer was the same defect with a
        different stored value.
        """
        backend({})
        database.create_terminal(
            terminal_id=DEAD_ID,
            tmux_session="cao-legacy",
            tmux_window="gone",
            provider="claude_code",
            pane_id="%10",
            window_id="@10",
            server_socket_path=SOCKET,
            session_id="$1",
            pane_pid=4242,
        )
        database.record_terminal_lifecycle(
            DEAD_ID, state="dead", reason="pane %10 is absent from its server"
        )

        response = client.get(f"/terminals/{DEAD_ID}")

        assert response.status_code == 200
        body = response.json()
        assert body["lifecycle_state"] == "dead"
        assert body["status"] == "dead"
        assert body["status"] not in PROVIDER_STATUSES


class TestCurrentRowsUnchanged:
    def test_a_live_row_still_reports_its_provider_status(
        self, client, isolated_memory_db, backend, monkeypatch
    ):
        """Machine orchestration polls this route for provider status; a
        live pane must answer with exactly that, as before."""
        backend({"%10": _pane_record()})
        monkeypatch.setattr(projection, "_provider_status", lambda _tid: "idle")
        database.create_terminal(
            terminal_id=LIVE_ID,
            tmux_session="cao-legacy",
            tmux_window="worker",
            provider="claude_code",
            pane_id="%10",
            window_id="@10",
            server_socket_path=SOCKET,
            session_id="$1",
            pane_pid=4242,
        )

        response = client.get(f"/terminals/{LIVE_ID}")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "idle"
        assert body["lifecycle_state"] == "live"
        assert body["lifecycle_reason"] is None


class TestSupportedDeletionPath:
    def test_the_legacy_row_remains_deletable_through_the_supported_endpoint(
        self, client, isolated_memory_db, backend, tmp_path, monkeypatch
    ):
        """B4's second half: renderable *and* still retired by DELETE.

        Deletion semantics are untouched by the fix — the supported
        endpoint stays the row's only deletion path, and after it runs the
        row is honestly absent (404), not hidden.
        """
        fake = backend({})
        _create_legacy_row(LEGACY_ID)
        monkeypatch.setattr(terminal_service, "TERMINAL_LOG_DIR", tmp_path)

        response = client.delete(f"/terminals/{LEGACY_ID}")

        assert response.status_code == 200
        assert response.json() == {"success": True}
        assert fake.killed == [("cao-legacy", "worker")]
        assert database.get_terminal_metadata(LEGACY_ID) is None
        assert client.get(f"/terminals/{LEGACY_ID}").status_code == 404
