"""The one projection both human views read, and what it refuses to guess.

Three defects motivate this suite, all observed together in one session:

* six terminal cards showed provider status ``Unknown`` forever, because
  nothing ever demoted a row whose window had been deleted and status was
  derived live — so "dead" and "alive but not yet detected" rendered
  identically;
* ``cao session status`` named a dead row, because it took ``terminals[0]``
  of the raw listing with no liveness filter, which is a guaranteed
  disagreement with the dashboard rather than a race;
* managed v2 workers appeared in neither view, because their rows live in
  a separate table by design.

The projection answers all three, and these tests hold it to the part
that is easy to get subtly wrong: liveness is *observed*, never inferred,
and an observation that failed is not an observation. A row that could
not be checked must not be promoted to live and must not be reaped —
"we could not look" is not evidence in either direction.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.terminal import TerminalLifecycleState
from cli_agent_orchestrator.services import terminal_projection as projection

SOCKET = "/private/tmp/cao-projection.sock"


class FakeBackend:
    """A backend that answers exactly one enumeration, or refuses to.

    ``panes=None`` models an unreadable tmux server, which is a state the
    projection has to distinguish from an empty one.
    """

    def __init__(self, panes: Optional[Dict[str, Dict[str, str]]]):
        self._panes = panes
        self.enumerations = 0

    @property
    def supports_pane_identity(self) -> bool:
        return True

    def observe_pane_identities(self) -> Optional[Dict[str, Dict[str, str]]]:
        self.enumerations += 1
        return self._panes


def _pane(
    pane_id: str = "%10",
    *,
    window_id: str = "@10",
    session_id: str = "$1",
    pane_pid: str = "4242",
    session_name: str = "cao-proj",
    window_name: str = "worker",
    dead: str = "0",
    socket: Optional[str] = SOCKET,
) -> Dict[str, str]:
    record = {
        "outcome": "observed",
        "pane_id": pane_id,
        "window_id": window_id,
        "session_id": session_id,
        "pane_pid": pane_pid,
        "session_name": session_name,
        "window_name": window_name,
        "dead": dead,
    }
    if socket is not None:
        record["server_socket_path"] = socket
    return record


def _row(terminal_id: str = "aaaa1111", **changes: Any) -> Dict[str, Any]:
    row = {
        "id": terminal_id,
        "tmux_session": "cao-proj",
        "tmux_window": "worker",
        "provider": "claude_code",
        "agent_profile": "developer",
        "generation": "gen-1",
        "pane_id": "%10",
        "window_id": "@10",
        "session_id": "$1",
        "pane_pid": 4242,
        "server_socket_path": SOCKET,
        "native_session_id": None,
        "last_active": None,
    }
    row.update(changes)
    return row


@pytest.fixture
def backend(monkeypatch):
    def _install(panes):
        fake = FakeBackend(panes)
        monkeypatch.setattr(projection, "get_backend", lambda: fake)
        return fake

    return _install


@pytest.fixture(autouse=True)
def _no_provider_status(monkeypatch):
    """Pin the provider's own answer so lifecycle is what is under test."""
    monkeypatch.setattr(projection, "_provider_status", lambda _tid: "idle")


class TestObservedLifecycle:
    def test_an_intact_identity_is_live(self, backend):
        fake = backend({"%10": _pane()})
        out = projection.project_row(_row(), fake.observe_pane_identities(), vintage="v1")
        assert out["lifecycle_state"] == projection.LIFECYCLE_LIVE
        assert out["status"] == "idle"

    def test_an_absent_pane_is_dead_not_unknown_status(self, backend):
        """The six phantom cards, in one assertion.

        A deleted window must report its lifecycle. Reporting provider
        ``unknown`` is what made a dead row indistinguishable from a
        healthy worker awaiting detection.
        """
        fake = backend({})
        out = projection.project_row(_row(), fake.observe_pane_identities(), vintage="v1")
        assert out["lifecycle_state"] == projection.LIFECYCLE_DEAD
        assert out["status"] == projection.LIFECYCLE_DEAD
        assert out["status"] != "unknown"

    def test_a_reused_pane_id_with_a_new_process_is_superseded(self, backend):
        fake = backend({"%10": _pane(pane_pid="9999")})
        out = projection.project_row(_row(), fake.observe_pane_identities(), vintage="v1")
        assert out["lifecycle_state"] == projection.LIFECYCLE_SUPERSEDED

    def test_a_pane_on_a_different_server_is_superseded(self, backend):
        """A pane id is unique only within one tmux server."""
        fake = backend({"%10": _pane(socket="/private/tmp/other.sock")})
        out = projection.project_row(_row(), fake.observe_pane_identities(), vintage="v1")
        assert out["lifecycle_state"] == projection.LIFECYCLE_SUPERSEDED

    def test_an_unreadable_server_is_unknown_liveness_not_dead(self, backend):
        """The distinction the whole classification turns on."""
        fake = backend(None)
        out = projection.project_row(_row(), fake.observe_pane_identities(), vintage="v1")
        assert out["lifecycle_state"] == projection.LIFECYCLE_UNKNOWN_LIVENESS
        assert out["lifecycle_state"] not in projection.ATTACHABLE_LIFECYCLE_STATES

    def test_a_row_with_no_recorded_identity_is_not_promoted_to_live(self, backend):
        """Having no evidence against a row is not evidence for it."""
        fake = backend({"%10": _pane()})
        bare = _row()
        for field in projection.terminal_service.IDENTITY_FIELDS:
            bare[field] = None

        out = projection.project_row(bare, fake.observe_pane_identities(), vintage="v1")

        assert out["lifecycle_state"] == projection.LIFECYCLE_UNKNOWN_LIVENESS
        assert out["lifecycle_reason"] == "no recorded identity"

    @pytest.mark.parametrize(
        "dropped", ["server_socket_path", "session_id", "window_id", "pane_id", "pane_pid"]
    )
    def test_a_partial_identity_is_never_rendered_live(self, backend, dropped):
        """The view applies the same rule as the write and attach paths.

        A row missing any component is not checked on the fields it does
        have. If the projection rendered it live, this would be the one
        surface left telling an operator that a row nothing else will
        accept is a healthy worker to click on.
        """
        fake = backend({"%10": _pane()})

        out = projection.project_row(
            _row(**{dropped: None}), fake.observe_pane_identities(), vintage="v1"
        )

        assert out["lifecycle_state"] == projection.LIFECYCLE_UNKNOWN_LIVENESS
        assert dropped in out["lifecycle_reason"]
        assert out["lifecycle_state"] not in projection.ATTACHABLE_LIFECYCLE_STATES

    def test_a_renamed_window_stays_live(self, backend):
        """Names are labels. Demoting on one would reap live workers."""
        fake = backend({"%10": _pane(window_name="renamed", session_name="cao-renamed")})
        out = projection.project_row(_row(), fake.observe_pane_identities(), vintage="v1")
        assert out["lifecycle_state"] == projection.LIFECYCLE_LIVE

    def test_the_stored_lifecycle_is_not_believed_over_the_observation(self, backend):
        """A stored state is what was true when it was written.

        A view that echoed it would keep showing a demoted row as live for
        as long as nobody happened to write to that terminal.
        """
        fake = backend({})
        out = projection.project_row(
            _row(lifecycle_state=projection.LIFECYCLE_LIVE),
            fake.observe_pane_identities(),
            vintage="v1",
        )
        assert out["lifecycle_state"] == projection.LIFECYCLE_DEAD


class TestOneInstantForTheWholeListing:
    def test_a_session_listing_enumerates_exactly_once(
        self, isolated_memory_db, backend, monkeypatch
    ):
        """Per-row probing would answer rows at different moments.

        A listing assembled that way can show one terminal live and
        another dead on the strength of two different instants, which is
        precisely the CLI/dashboard disagreement being removed.
        """
        fake = backend({"%10": _pane()})
        for index in range(3):
            database.create_terminal(
                terminal_id=f"bbbb{index}{index}{index}{index}",
                tmux_session="cao-proj",
                tmux_window=f"w{index}",
                provider="claude_code",
                pane_id="%10",
                window_id="@10",
                session_id="$1",
                pane_pid=4242,
                server_socket_path=SOCKET,
            )

        rows = projection.project_session("cao-proj")

        assert len(rows) == 3
        assert fake.enumerations == 1


class TestVintageBoundary:
    def test_a_v2_row_is_visible_and_labelled(self, backend):
        """Human visibility is additive; the write boundary is untouched."""
        fake = backend({"%10": _pane()})
        v2_row = {
            "id": "cccc2222",
            "tmux_session": "cao-proj",
            "tmux_window": "managed",
            "provider": "claude_code",
            "generation": "gen-v2",
            "caller_id": "deadbeef",
            "pane_id": "%10",
            "window_id": "@10",
            "server_socket_path": SOCKET,
            "v2_session_id": "$1",
            "v2_pane_pid": 4242,
            "v2_native_session_id": "6d1f0e34-0000-4000-8000-00000000abcd",
            "v2_lifecycle_state": None,
            "v2_lifecycle_reason": None,
            "v2_superseded_by_terminal_id": None,
            "v2_superseded_by_generation": None,
            "last_active": None,
        }

        out = projection.project_row(v2_row, fake.observe_pane_identities(), vintage="v2")

        assert out["protocol_vintage"] == "v2"
        assert out["caller_id"] == "deadbeef"
        assert out["lifecycle_state"] == projection.LIFECYCLE_LIVE
        # The v2 store prefixes these columns so the vintage receipt can
        # require unique bare names. That prefix is storage detail and
        # must not reach a view.
        assert out["session_id"] == "$1"
        assert out["pane_pid"] == 4242
        assert out["native_session_id"] == "6d1f0e34-0000-4000-8000-00000000abcd"
        assert not any(key.startswith("v2_") for key in out)

    def test_both_vintages_render_the_same_fields(self, backend):
        """The agreement invariant, asserted as a field-set equality.

        Two views can only render identical rows if the rows themselves
        are identically shaped, whichever store they came from.
        """
        fake = backend({"%10": _pane()})
        panes = fake.observe_pane_identities()
        v1 = projection.project_row(_row(), panes, vintage="v1")
        v2 = projection.project_row(
            {**_row("dddd3333"), "v2_session_id": "$1", "v2_pane_pid": 4242},
            panes,
            vintage="v2",
        )
        assert set(v1) == set(v2)
        for field in projection.PROJECTION_FIELDS:
            assert field in v1


class TestLiveResolution:
    def test_demoted_rows_are_excluded_not_ranked_last(
        self, isolated_memory_db, backend, monkeypatch
    ):
        """Ranking still picks a dead row when that is all there is."""
        fake = backend({"%11": _pane(pane_id="%11", window_id="@11")})
        database.create_terminal(
            terminal_id="eeee4444",
            tmux_session="cao-proj",
            tmux_window="gone",
            provider="claude_code",
            pane_id="%10",
            window_id="@10",
            session_id="$1",
            pane_pid=4242,
            server_socket_path=SOCKET,
        )
        database.create_terminal(
            terminal_id="ffff5555",
            tmux_session="cao-proj",
            tmux_window="alive",
            provider="claude_code",
            pane_id="%11",
            window_id="@11",
            session_id="$1",
            pane_pid=4242,
            server_socket_path=SOCKET,
        )

        live = projection.live_terminals("cao-proj")

        assert [row["terminal_id"] for row in live] == ["ffff5555"]

    def test_no_live_rows_yields_an_empty_list_not_a_substitute(self, isolated_memory_db, backend):
        backend({})
        database.create_terminal(
            terminal_id="9999aaaa",
            tmux_session="cao-proj",
            tmux_window="gone",
            provider="claude_code",
            pane_id="%10",
            window_id="@10",
            session_id="$1",
            pane_pid=4242,
            server_socket_path=SOCKET,
        )

        assert projection.live_terminals("cao-proj") == []


class TestSingleTerminalLookup:
    """One terminal by id, from whichever store holds it.

    The lookup is vintage-aware for the same reason the listing is: a
    managed v2 worker lives in a separate table by design, and a reader
    that only knew about the v1 one would report it absent rather than
    tell the truth about where it is.
    """

    def test_a_v1_row_is_found_and_projected(self, isolated_memory_db, backend):
        backend({"%10": _pane()})
        database.create_terminal(
            terminal_id="1111bbbb",
            tmux_session="cao-proj",
            tmux_window="worker",
            provider="claude_code",
            pane_id="%10",
            window_id="@10",
            session_id="$1",
            pane_pid=4242,
            server_socket_path=SOCKET,
        )

        out = projection.project_terminal("1111bbbb")

        assert out["terminal_id"] == "1111bbbb"
        assert out["protocol_vintage"] == "v1"
        assert out["lifecycle_state"] == projection.LIFECYCLE_LIVE

    def test_a_terminal_in_neither_store_is_absent(self, isolated_memory_db, backend):
        backend({})
        assert projection.project_terminal("no-such-terminal") is None

    def test_a_dead_v1_row_still_resolves_and_reports_its_lifecycle(
        self, isolated_memory_db, backend
    ):
        """Absent is a fact about the pane, not about the record.

        The row is still findable — that is what lets a human see *why* the
        card is gone instead of the terminal simply vanishing.
        """
        backend({})
        database.create_terminal(
            terminal_id="2222cccc",
            tmux_session="cao-proj",
            tmux_window="worker",
            provider="claude_code",
            pane_id="%10",
            window_id="@10",
            session_id="$1",
            pane_pid=4242,
            server_socket_path=SOCKET,
        )

        out = projection.project_terminal("2222cccc")

        assert out["lifecycle_state"] == projection.LIFECYCLE_DEAD
        assert out["status"] == projection.LIFECYCLE_DEAD


class TestLifecycleVocabularyDrift:
    def test_the_projection_vocabulary_is_exactly_the_response_models(self):
        """A lifecycle the projection emits but the model cannot admit.

        ``Terminal.status`` declares exactly ``TerminalLifecycleState``'s
        values at the response boundary; a projection-only addition would
        fail response validation there, and an existing legacy terminal row
        would read as a 404 again — the cond-0173 failure this guards.
        """
        assert {
            projection.LIFECYCLE_LIVE,
            projection.LIFECYCLE_SUPERSEDED,
            projection.LIFECYCLE_DEAD,
            projection.LIFECYCLE_UNKNOWN_LIVENESS,
        } == {state.value for state in TerminalLifecycleState}
