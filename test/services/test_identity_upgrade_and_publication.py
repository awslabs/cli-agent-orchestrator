"""Installing this build over an existing database, and what it publishes.

The identity boundary added two fields to the canonical tuple. The build
deployed before it wrote three of the five, so on the day this is
installed every pre-existing row grades *partial* — and partial fails
closed. Nothing would be misdelivered, but the running fleet would refuse
control input, be unreadable, and be unattachable, all at once. That is
the cutover this suite is about.

The other half is publication. The conductor's non-adoptable gate reads
``lifecycle_state`` off the v2 reservation response; while that key was
absent the gate compared ``None`` against the non-adoptable set, never
matched, and fell through to the adoption branches. A guard that fails
open is worse than no guard, because the code reads as though the case is
handled — so these tests check the keys are *always present*, not merely
present when they happen to have a value.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import terminal_projection as projection
from cli_agent_orchestrator.services import terminal_service

SOCKET = "/private/tmp/cao-upgrade.sock"
OTHER_SOCKET = "/private/tmp/cao-elsewhere.sock"


class FakeBackend:
    """Answers pane observations, and records what it was asked to write."""

    def __init__(self, panes: Optional[Dict[str, Dict[str, Any]]] = None):
        self._panes = panes if panes is not None else {}
        self.special_keys: list[tuple] = []

    @property
    def supports_pane_identity(self) -> bool:
        return True

    def observe_pane_identities(self) -> Optional[Dict[str, Dict[str, Any]]]:
        return dict(self._panes)

    def observe_pane_identity(self, pane_id: str) -> Dict[str, Any]:
        pane = self._panes.get(pane_id)
        if pane is None:
            return {"outcome": "absent", "pane_id": pane_id}
        return dict(pane)

    def send_special_key(self, session_name, window_name, key, pane_id=None):
        self.special_keys.append((session_name, window_name, key, pane_id))


def _pane(
    pane_id: str = "%10",
    *,
    window_id: str = "@10",
    session_id: str = "$1",
    pane_pid: str = "4242",
    session_name: str = "cao-up",
    window_name: str = "worker",
    socket: str = SOCKET,
    dead: str = "0",
) -> Dict[str, Any]:
    return {
        "outcome": "observed",
        "pane_id": pane_id,
        "window_id": window_id,
        "session_id": session_id,
        "pane_pid": pane_pid,
        "session_name": session_name,
        "window_name": window_name,
        "server_socket_path": socket,
        "dead": dead,
    }


@pytest.fixture
def backend(monkeypatch):
    def _install(panes=None):
        fake = FakeBackend(panes)
        for module in (terminal_service, projection):
            monkeypatch.setattr(module, "get_backend", lambda: fake)
        return fake

    return _install


def _deployed_build_row(terminal_id: str = "aaaa1111", **changes: Any) -> None:
    """A row exactly as the previously deployed build wrote it.

    Three of the five identity fields, with ``session_id`` and ``pane_pid``
    NULL — which is what the migration leaves, because it explicitly
    declines to backfill values it would have to guess.
    """
    kwargs = {
        "terminal_id": terminal_id,
        "tmux_session": "cao-up",
        "tmux_window": "worker",
        "provider": "claude_code",
        "pane_id": "%10",
        "window_id": "@10",
        "server_socket_path": SOCKET,
    }
    kwargs.update(changes)
    database.create_terminal(**kwargs)


class TestObservedUpgrade:
    def test_a_three_of_five_row_is_completed_from_its_own_pane(self, isolated_memory_db, backend):
        """The cutover, in one test.

        The values are read from the row's own recorded pane on the row's
        own recorded socket — an observation, not the guess the migration
        rightly refuses to make.
        """
        backend({"%10": _pane()})
        _deployed_build_row()

        metadata = database.get_terminal_metadata("aaaa1111")
        assert terminal_service.identity_completeness(metadata) == terminal_service.IDENTITY_PARTIAL

        target = terminal_service.verified_pane_target(
            "aaaa1111", metadata, operation="control input"
        )

        assert target is not None
        assert target.pane_id == "%10"
        upgraded = database.get_terminal_metadata("aaaa1111")
        assert upgraded["session_id"] == "$1"
        assert upgraded["pane_pid"] == 4242
        assert (
            terminal_service.identity_completeness(upgraded) == terminal_service.IDENTITY_COMPLETE
        )

    def test_the_upgrade_is_persisted_not_merely_returned(self, isolated_memory_db, backend):
        """A per-read repair would re-observe on every call and never settle."""
        backend({"%10": _pane()})
        _deployed_build_row()

        terminal_service.upgrade_observed_identity(
            "aaaa1111", database.get_terminal_metadata("aaaa1111")
        )

        assert database.get_terminal_metadata("aaaa1111")["pane_pid"] == 4242

    def test_a_pane_that_is_gone_is_not_upgraded_and_the_row_still_refuses(
        self, isolated_memory_db, backend
    ):
        """Nothing observed, nothing written, nothing delivered."""
        backend({})
        _deployed_build_row()

        with pytest.raises(terminal_service.TerminalIdentityMismatchError):
            terminal_service.verified_pane_target(
                "aaaa1111", database.get_terminal_metadata("aaaa1111"), operation="control input"
            )
        assert database.get_terminal_metadata("aaaa1111")["session_id"] is None

    def test_an_observation_from_a_different_server_is_refused(self, isolated_memory_db, backend):
        """A pane id is unique only within one server.

        Accepting a record from elsewhere is the exact mistake the socket
        was added to the tuple to prevent — and it would be recorded as
        this row's identity, so every later check would confirm it.
        """
        backend({"%10": _pane(socket=OTHER_SOCKET)})
        _deployed_build_row()

        assert (
            terminal_service.upgrade_observed_identity(
                "aaaa1111", database.get_terminal_metadata("aaaa1111")
            )
            is None
        )
        assert database.get_terminal_metadata("aaaa1111")["session_id"] is None

    def test_a_row_with_no_socket_is_never_upgraded(self, isolated_memory_db, backend):
        """There is nothing to observe it against.

        This is precisely the case where a guess would put a live handle on
        somebody else's pane: a restarted server reissues ``%0``/``%1``.
        """
        backend({"%10": _pane()})
        _deployed_build_row("bbbb2222", server_socket_path=None)

        assert (
            terminal_service.upgrade_observed_identity(
                "bbbb2222", database.get_terminal_metadata("bbbb2222")
            )
            is None
        )

    def test_the_upgrade_cannot_rewrite_a_row_that_is_already_complete(
        self, isolated_memory_db, backend
    ):
        """The write matches on both fields being NULL.

        So a complete row is never re-pointed, and two concurrent upgrades
        cannot both win.
        """
        backend({"%10": _pane()})
        _deployed_build_row()
        database.upgrade_terminal_identity_from_observation(
            "aaaa1111",
            pane_id="%10",
            server_socket_path=SOCKET,
            session_id="$1",
            pane_pid=4242,
        )

        second = database.upgrade_terminal_identity_from_observation(
            "aaaa1111",
            pane_id="%10",
            server_socket_path=SOCKET,
            session_id="$9",
            pane_pid=9999,
        )

        assert second is False
        assert database.get_terminal_metadata("aaaa1111")["session_id"] == "$1"

    def test_the_projection_also_upgrades_rather_than_grading_everything_unknown(
        self, isolated_memory_db, backend
    ):
        """Both readers have to agree, or a row is live in one view and not the other."""
        backend({"%10": _pane()})
        _deployed_build_row()

        projected = projection.project_terminal("aaaa1111")

        assert projected["lifecycle_state"] == projection.LIFECYCLE_LIVE
        assert projected["session_id"] == "$1"


def _deployed_v2_row(terminal_id: str = "cccc3333", **changes: Any) -> None:
    """A *managed* row exactly as the previously deployed build wrote it.

    The same three-of-five shape as :func:`_deployed_build_row`, in the
    isolated v2 store. The managed fleet is the half that must survive the
    cutover intact: these are the workers a conductor preserved across the
    upgrade precisely so it would not have to respawn them.
    """
    kwargs = {
        "terminal_id": terminal_id,
        "tmux_session": "cao-up",
        "tmux_window": "worker",
        "provider": "claude_code",
        "generation": f"gen-{terminal_id}",
        "pane_id": "%10",
        "window_id": "@10",
        "server_socket_path": SOCKET,
    }
    kwargs.update(changes)
    database.create_terminal_v2(**kwargs)


class TestV2ObservedUpgrade:
    """The managed store gets the same cutover, through its own columns.

    A v2 row handed to the shared-table writer matches zero rows — the id
    is not in ``terminals`` at all — so the write reports failure, the
    upgrade never lands, and every preserved managed worker is graded
    ``unknown-liveness`` on every read, forever. Fail-closed applied to
    rows a single observation could have answered is still a dead fleet.
    """

    def test_a_managed_three_of_five_row_is_completed_from_its_own_pane(
        self, isolated_memory_db, backend
    ):
        backend({"%10": _pane()})
        _deployed_v2_row()

        metadata = database.get_terminal_metadata_v2("cccc3333")
        assert metadata["v2_session_id"] is None

        upgraded = terminal_service.upgrade_observed_identity(
            "cccc3333",
            {**metadata, "session_id": None, "pane_pid": None},
        )

        assert upgraded is not None
        stored = database.get_terminal_metadata_v2("cccc3333")
        assert stored["v2_session_id"] == "$1"
        assert stored["v2_pane_pid"] == 4242

    def test_the_managed_row_is_written_through_the_v2_columns_only(
        self, isolated_memory_db, backend
    ):
        """The isolation invariant is not relaxed to make the upgrade work.

        A managed row completed by writing into the shared table would
        make an old-binary list/watchdog/cleanup path able to see a v2
        terminal, which is the one thing the split store exists to
        prevent.
        """
        backend({"%10": _pane()})
        _deployed_v2_row()

        terminal_service.upgrade_observed_identity(
            "cccc3333",
            {**database.get_terminal_metadata_v2("cccc3333"), "session_id": None, "pane_pid": None},
        )

        assert database.get_terminal_metadata("cccc3333") is None

    def test_the_projection_recovers_a_preserved_managed_worker(self, isolated_memory_db, backend):
        """End to end, through the reader that actually demoted the fleet.

        The projection is where the damage showed: it graded the row
        partial, called the upgrade, got nothing back, and published
        ``unknown-liveness`` for a pane that was running the whole time.
        """
        backend({"%10": _pane()})
        _deployed_v2_row()

        projected = projection.project_terminal("cccc3333")

        assert projected["lifecycle_state"] == projection.LIFECYCLE_LIVE
        assert projected["session_id"] == "$1"
        assert projected["pane_pid"] == 4242
        assert projected["protocol_vintage"] == "v2"

    def test_a_managed_row_whose_pane_moved_servers_is_not_upgraded(
        self, isolated_memory_db, backend
    ):
        """Observed on a different socket is a different pane wearing the id."""
        backend({"%10": _pane(socket=OTHER_SOCKET)})
        _deployed_v2_row()

        assert (
            terminal_service.upgrade_observed_identity(
                "cccc3333",
                {
                    **database.get_terminal_metadata_v2("cccc3333"),
                    "session_id": None,
                    "pane_pid": None,
                },
            )
            is None
        )
        assert database.get_terminal_metadata_v2("cccc3333")["v2_session_id"] is None

    def test_the_write_refuses_a_socket_that_is_not_the_rows_own(self, isolated_memory_db):
        """The CAS itself carries the guard, not only the caller above it.

        The service checks the observed socket before writing, but the
        writer is reachable on its own; a guard that lives only in the
        caller is one new call site away from being absent.
        """
        _deployed_v2_row()

        assert (
            database.upgrade_v2_terminal_identity_from_observation(
                "cccc3333",
                pane_id="%10",
                server_socket_path=OTHER_SOCKET,
                session_id="$1",
                pane_pid=4242,
            )
            is False
        )
        assert database.get_terminal_metadata_v2("cccc3333")["v2_session_id"] is None

    def test_a_managed_row_that_is_already_complete_is_never_rewritten(self, isolated_memory_db):
        """Matched on both v2 columns being NULL.

        So the second of two concurrent upgrades loses rather than
        re-pointing a row that the first one already answered.
        """
        _deployed_v2_row()
        first = database.upgrade_v2_terminal_identity_from_observation(
            "cccc3333",
            pane_id="%10",
            server_socket_path=SOCKET,
            session_id="$1",
            pane_pid=4242,
        )
        second = database.upgrade_v2_terminal_identity_from_observation(
            "cccc3333",
            pane_id="%10",
            server_socket_path=SOCKET,
            session_id="$9",
            pane_pid=9999,
        )

        assert (first, second) == (True, False)
        stored = database.get_terminal_metadata_v2("cccc3333")
        assert (stored["v2_session_id"], stored["v2_pane_pid"]) == ("$1", 4242)

    def test_a_v1_row_is_untouched_by_the_v2_writer_and_the_reverse(self, isolated_memory_db):
        """Neither writer can reach the other store.

        Same id in both tables is the sharpest form of the question: if
        either writer chose its table by anything other than the vintage
        it was given, one of these assertions moves.
        """
        _deployed_build_row("dddd4444")
        _deployed_v2_row("dddd4444")

        assert (
            database.upgrade_v2_terminal_identity_from_observation(
                "dddd4444",
                pane_id="%10",
                server_socket_path=SOCKET,
                session_id="$2",
                pane_pid=777,
            )
            is True
        )

        assert database.get_terminal_metadata("dddd4444")["session_id"] is None
        assert database.get_terminal_metadata_v2("dddd4444")["v2_session_id"] == "$2"

    def test_the_vintage_decides_the_writer(self, isolated_memory_db, backend, monkeypatch):
        """Dispatch is on the row's own ``protocol_vintage``.

        Asserted by watching which writer is called, because both stores
        answering correctly at the end could also be produced by writing
        to both — and that would breach the isolation invariant while
        every value-level assertion still passed.
        """
        backend({"%10": _pane()})
        _deployed_v2_row()
        called: list[str] = []

        for name in (
            "upgrade_terminal_identity_from_observation",
            "upgrade_v2_terminal_identity_from_observation",
        ):
            monkeypatch.setattr(
                terminal_service,
                name,
                lambda *a, _n=name, **k: called.append(_n) or True,
            )

        terminal_service.upgrade_observed_identity(
            "cccc3333",
            {**database.get_terminal_metadata_v2("cccc3333"), "session_id": None, "pane_pid": None},
        )

        assert called == ["upgrade_v2_terminal_identity_from_observation"]


class _PipelineBackend(FakeBackend):
    """A backend double that also answers the output-pipeline calls."""

    def __init__(self, panes=None):
        super().__init__(panes)
        self.piped: list[tuple] = []
        self.stopped: list[tuple] = []
        self.history: list[tuple] = []

    def supports_event_inbox(self) -> bool:
        return False

    def get_history(self, session_name, window_name, tail_lines=None, **kwargs) -> str:
        self.history.append((session_name, window_name))
        return ""

    def pipe_pane(self, session_name, window_name, path) -> None:
        self.piped.append((session_name, window_name, path))

    def stop_pipe_pane(self, session_name, window_name) -> None:
        self.stopped.append((session_name, window_name))


class TestStartupReattachIsIdentityBound:
    """Server-restart recovery types into a proven pane or into none.

    This is the path where the recorded names are least trustworthy: it
    runs against rows written by a *previous* process, and a session and
    window torn down while the server was dead and recreated under the
    same names resolve perfectly. The unverified form both nudged Enter
    into whatever answered those names and piped that pane's output into
    this row's FIFO, where it was then read as this terminal's provider
    status.
    """

    @pytest.fixture
    def pipeline(self, monkeypatch):
        def _install(panes=None):
            fake = _PipelineBackend(panes)
            monkeypatch.setattr(terminal_service, "get_backend", lambda: fake)
            monkeypatch.setattr(
                terminal_service.fifo_manager,
                "create_reader",
                lambda *a, **k: None,
            )
            return fake

        return _install

    def _complete_row(self, terminal_id="eeee5555", **changes):
        kwargs = {
            "terminal_id": terminal_id,
            "tmux_session": "cao-up",
            "tmux_window": "worker",
            "provider": "claude_code",
            "pane_id": "%10",
            "window_id": "@10",
            "server_socket_path": SOCKET,
            "session_id": "$1",
            "pane_pid": 4242,
        }
        kwargs.update(changes)
        database.create_terminal(**kwargs)

    def test_a_reused_name_gets_no_enter_and_no_pipe(self, isolated_memory_db, pipeline):
        """The row's own pane is gone; something else answers to its names.

        Nothing is delivered and nothing is piped — the row is skipped,
        which is the only outcome that does not act on a stranger's pane.
        """
        backend = pipeline({"%77": _pane(pane_id="%77")})
        self._complete_row()

        result = terminal_service.reattach_existing_output_pipelines()

        assert result == {"reattached": [], "skipped": ["eeee5555"]}
        assert backend.special_keys == []
        assert backend.piped == []

    def test_a_proven_pane_receives_enter_at_its_exact_pane_id(self, isolated_memory_db, pipeline):
        backend = pipeline({"%10": _pane()})
        self._complete_row()

        result = terminal_service.reattach_existing_output_pipelines()

        assert result["reattached"] == ["eeee5555"]
        assert [key[2:] for key in backend.special_keys] == [("Enter", "%10")]

    def test_the_pipe_follows_the_verified_pane_after_a_rename(self, isolated_memory_db, pipeline):
        """Names are mutable, so the recorded ones are not addressed.

        ``pipe-pane`` and history are name-shaped, so they use the names
        the *verified* pane answers to now. Using the row's recorded names
        would re-open the hole the proof just closed, one call later.
        """
        backend = pipeline({"%10": _pane(session_name="cao-renamed", window_name="worker-renamed")})
        self._complete_row()

        terminal_service.reattach_existing_output_pipelines()

        assert backend.piped and backend.piped[0][:2] == ("cao-renamed", "worker-renamed")
        assert backend.stopped[0][:2] == ("cao-renamed", "worker-renamed")

    def test_a_row_with_no_recorded_identity_still_resolves_by_name(
        self, isolated_memory_db, pipeline
    ):
        """The documented boundary, not an exemption.

        A row that never recorded a pane has nothing to prove against;
        refusing it would break restart recovery for every terminal that
        predates the identity fields, and would do so without making any
        delivery safer.
        """
        backend = pipeline({"%10": _pane()})
        database.create_terminal(
            terminal_id="ffff6666",
            tmux_session="cao-up",
            tmux_window="legacy",
            provider="claude_code",
        )

        result = terminal_service.reattach_existing_output_pipelines()

        assert result["reattached"] == ["ffff6666"]
        assert backend.special_keys == [("cao-up", "legacy", "Enter", None)]


class TestSupersessionNamesItsSuccessor:
    def test_a_demoted_row_records_which_terminal_now_holds_its_pane(
        self, isolated_memory_db, backend
    ):
        """A row that can only say it lost its pane is unactionable.

        The next action — read the superseding terminal's identity, then
        replace — has to be answerable from the row itself.
        """
        backend({"%10": _pane(pane_pid="7777")})
        # The old incarnation, registered to the pane as it used to be.
        database.create_terminal(
            terminal_id="0ld00000",
            tmux_session="cao-up",
            tmux_window="worker",
            provider="claude_code",
            generation="gen-old",
            pane_id="%10",
            window_id="@10",
            session_id="$1",
            pane_pid=4242,
            server_socket_path=SOCKET,
        )
        # The new one, holding the pane as it now stands.
        database.create_terminal(
            terminal_id="new00000",
            tmux_session="cao-up",
            tmux_window="worker",
            provider="claude_code",
            generation="gen-new",
            pane_id="%10",
            window_id="@10",
            session_id="$1",
            pane_pid=7777,
            server_socket_path=SOCKET,
        )

        with pytest.raises(terminal_service.TerminalIdentityMismatchError) as excinfo:
            terminal_service.verified_pane_target(
                "0ld00000",
                database.get_terminal_metadata("0ld00000"),
                operation="control input",
            )

        # Named in the refusal, so an operator reading the error alone can act.
        assert "new00000" in str(excinfo.value)
        row = database.get_terminal_metadata("0ld00000")
        assert row["lifecycle_state"] == terminal_service.LIFECYCLE_SUPERSEDED
        assert row["superseded_by_terminal_id"] == "new00000"
        assert row["superseded_by_generation"] == "gen-new"
        assert projection.project_terminal("0ld00000")["superseded_by_terminal_id"] == "new00000"

    def test_a_pane_nobody_registered_leaves_the_pointer_null(self, isolated_memory_db, backend):
        """No match is a normal answer, and inventing one would be worse.

        The pane may belong to something this system never registered; the
        demotion records the loss without naming a destination for it.
        """
        backend({"%10": _pane(pane_pid="7777")})
        database.create_terminal(
            terminal_id="0ld00000",
            tmux_session="cao-up",
            tmux_window="worker",
            provider="claude_code",
            pane_id="%10",
            window_id="@10",
            session_id="$1",
            pane_pid=4242,
            server_socket_path=SOCKET,
        )

        with pytest.raises(terminal_service.TerminalIdentityMismatchError):
            terminal_service.verified_pane_target(
                "0ld00000",
                database.get_terminal_metadata("0ld00000"),
                operation="control input",
            )

        row = database.get_terminal_metadata("0ld00000")
        assert row["lifecycle_state"] == terminal_service.LIFECYCLE_SUPERSEDED
        assert row["superseded_by_terminal_id"] is None


class TestRegistrationAndNativeSession:
    def test_registration_is_idempotent_and_refuses_to_re_point(self, isolated_memory_db):
        """Re-driving a registration is how recovery works, so it has to be free."""
        database.create_terminal(
            terminal_id="cccc3333",
            tmux_session="cao-up",
            tmux_window="worker",
            provider="claude_code",
            generation="gen-1",
            pane_id="%10",
            window_id="@10",
            session_id="$1",
            pane_pid=4242,
            server_socket_path=SOCKET,
        )
        identity = {
            "server_socket_path": SOCKET,
            "session_id": "$1",
            "window_id": "@10",
            "pane_id": "%10",
            "pane_pid": 4242,
        }

        assert terminal_service._register_incarnation("cccc3333", "gen-1", identity) is True
        assert terminal_service._register_incarnation("cccc3333", "gen-1", identity) is True

        moved = {**identity, "pane_id": "%99"}
        assert terminal_service._register_incarnation("cccc3333", "gen-1", moved) is False
        # Left exactly as it was: overwriting would move a handle from the
        # pane somebody registered onto one they did not.
        assert database.get_terminal_metadata("cccc3333")["pane_id"] == "%10"

    def test_creating_a_terminal_registers_it(self, isolated_memory_db, backend):
        """The create path and a later re-drive go through the same verb.

        Otherwise the idempotency and never-re-point guarantees are
        promises nothing exercises.
        """
        backend({"%10": _pane()})
        database.create_terminal(
            terminal_id="dddd4444",
            tmux_session="cao-up",
            tmux_window="worker",
            provider="claude_code",
            generation="gen-2",
            pane_id="%10",
            window_id="@10",
            session_id="$1",
            pane_pid=4242,
            server_socket_path=SOCKET,
        )

        registered = terminal_service._register_incarnation(
            "dddd4444",
            "gen-2",
            {
                "server_socket_path": SOCKET,
                "session_id": "$1",
                "window_id": "@10",
                "pane_id": "%10",
                "pane_pid": 4242,
            },
        )

        assert registered is True
        assert database.get_terminal_metadata("dddd4444")["lifecycle_state"] == "live"

    def test_a_native_session_is_recorded_and_never_re_pointed(self, isolated_memory_db):
        """A pane running someone else's session is a supersession, not an update."""
        database.create_terminal(
            terminal_id="eeee5555",
            tmux_session="cao-up",
            tmux_window="worker",
            provider="claude_code",
            pane_id="%10",
            window_id="@10",
            session_id="$1",
            pane_pid=4242,
            server_socket_path=SOCKET,
        )

        assert terminal_service.record_native_session("eeee5555", "uuid-one") is True
        assert database.get_terminal_metadata("eeee5555")["native_session_id"] == "uuid-one"
        # Idempotent for the same session, refused for a different one.
        assert terminal_service.record_native_session("eeee5555", "uuid-one") is True
        assert terminal_service.record_native_session("eeee5555", "uuid-two") is False
        assert database.get_terminal_metadata("eeee5555")["native_session_id"] == "uuid-one"

    def test_the_projection_carries_the_native_session(self, isolated_memory_db, backend):
        backend({"%10": _pane()})
        database.create_terminal(
            terminal_id="ffff6666",
            tmux_session="cao-up",
            tmux_window="worker",
            provider="claude_code",
            pane_id="%10",
            window_id="@10",
            session_id="$1",
            pane_pid=4242,
            server_socket_path=SOCKET,
        )
        terminal_service.record_native_session("ffff6666", "6d1f0e34-0000-4000-8000-00000000abcd")

        projected = projection.project_terminal("ffff6666")

        assert projected["native_session_id"] == "6d1f0e34-0000-4000-8000-00000000abcd"


class TestSpecialKeyIsIdentityBound:
    def test_a_control_key_reaches_the_verified_pane(self, isolated_memory_db, backend):
        fake = backend({"%10": _pane()})
        database.create_terminal(
            terminal_id="7777aaaa",
            tmux_session="cao-up",
            tmux_window="worker",
            provider="claude_code",
            pane_id="%10",
            window_id="@10",
            session_id="$1",
            pane_pid=4242,
            server_socket_path=SOCKET,
        )

        assert terminal_service.send_special_key("7777aaaa", "C-c") is True

        assert fake.special_keys == [("cao-up", "worker", "C-c", "%10")]

    def test_a_reused_window_name_receives_no_key_at_all(self, isolated_memory_db, backend):
        """``Enter`` submits and ``C-c`` interrupts the instant they land.

        Delivered to a window resolved by name after that name was reused,
        both act on a stranger's session — and neither leaves the trace a
        mistyped message would.
        """
        # The row's pane is gone; an unrelated window now answers to its name.
        fake = backend({"%99": _pane("%99", window_id="@99", pane_pid="5555")})
        database.create_terminal(
            terminal_id="8888bbbb",
            tmux_session="cao-up",
            tmux_window="worker",
            provider="claude_code",
            pane_id="%10",
            window_id="@10",
            session_id="$1",
            pane_pid=4242,
            server_socket_path=SOCKET,
        )

        with pytest.raises(terminal_service.TerminalIdentityMismatchError):
            terminal_service.send_special_key("8888bbbb", "Enter")

        assert fake.special_keys == []

    def test_a_row_with_no_identity_keeps_the_name_path(self, isolated_memory_db, backend):
        """The documented boundary: a legacy row is not broken by this."""
        fake = backend({})
        database.create_terminal(
            terminal_id="9999cccc",
            tmux_session="cao-up",
            tmux_window="legacy",
            provider="claude_code",
        )

        assert terminal_service.send_special_key("9999cccc", "C-d") is True

        assert fake.special_keys == [("cao-up", "legacy", "C-d", None)]


class TestV2ReservationPublishesWhatTheConductorReads:
    """The paired surface. Field names are the agreement, so they are asserted.

    The conductor's non-adoptable gate reads ``lifecycle_state`` here. While
    the key was absent it compared ``None`` against the non-adoptable set,
    never matched, and fell through to the adoption branches — a guard that
    fails open, which is worse than none because the code reads as handled.
    """

    def test_every_published_key_is_present_even_with_no_terminal(self):
        """Absent and null mean opposite things and must not look alike.

        Absent is "this peer cannot answer" and has to fail closed; null is
        "this peer answered, and the row holds nothing".
        """
        from cli_agent_orchestrator.services import managed_launch_v2 as v2

        published = v2._published_terminal_facts(None)

        assert set(published) == set(v2.PUBLISHED_TERMINAL_FIELDS)
        assert all(value is None for value in published.values())

    def test_the_published_identity_is_the_six_field_tuple(self):
        """Six, not the five the writer boundary checks.

        A conductor deciding whether to adopt a pane also has to know which
        provider session is running in it: a pane running a *different*
        session than the one registered is a supersession the other five
        fields cannot see.
        """
        from cli_agent_orchestrator.services import managed_launch_v2 as v2

        assert v2.PUBLISHED_IDENTITY_FIELDS == (
            "server_socket_path",
            "session_id",
            "window_id",
            "pane_id",
            "pane_pid",
            "native_session_id",
        )
        for field in v2.PUBLISHED_IDENTITY_FIELDS:
            assert field in v2.PUBLISHED_TERMINAL_FIELDS

    def test_the_successor_pointer_is_published_under_its_two_exact_names(self):
        """One spelling, agreed with the conductor. A rename renders nothing."""
        from cli_agent_orchestrator.services import managed_launch_v2 as v2

        assert "superseded_by_terminal_id" in v2.PUBLISHED_TERMINAL_FIELDS
        assert "superseded_by_generation" in v2.PUBLISHED_TERMINAL_FIELDS

    def test_a_live_terminals_facts_reach_the_reservation_surface(
        self, isolated_memory_db, backend
    ):
        from cli_agent_orchestrator.services import managed_launch_v2 as v2

        backend({"%10": _pane()})
        database.create_terminal(
            terminal_id="aabb1122",
            tmux_session="cao-up",
            tmux_window="worker",
            provider="claude_code",
            generation="gen-live",
            pane_id="%10",
            window_id="@10",
            session_id="$1",
            pane_pid=4242,
            server_socket_path=SOCKET,
        )
        terminal_service.record_native_session("aabb1122", "uuid-live")

        published = v2._published_terminal_facts("aabb1122")

        assert published["lifecycle_state"] == projection.LIFECYCLE_LIVE
        assert published["pane_id"] == "%10"
        assert published["pane_pid"] == 4242
        assert published["server_socket_path"] == SOCKET
        assert published["native_session_id"] == "uuid-live"

    def test_a_dead_terminal_publishes_a_lifecycle_the_gate_can_match(
        self, isolated_memory_db, backend
    ):
        """This is the value the non-adoptable gate exists to see."""
        from cli_agent_orchestrator.services import managed_launch_v2 as v2

        backend({})
        database.create_terminal(
            terminal_id="ccdd3344",
            tmux_session="cao-up",
            tmux_window="worker",
            provider="claude_code",
            pane_id="%10",
            window_id="@10",
            session_id="$1",
            pane_pid=4242,
            server_socket_path=SOCKET,
        )

        published = v2._published_terminal_facts("ccdd3344")

        assert published["lifecycle_state"] == projection.LIFECYCLE_DEAD

    def test_an_unreadable_terminal_publishes_nulls_rather_than_raising(
        self, isolated_memory_db, monkeypatch
    ):
        """A reservation response is not the place to discover a projection bug."""
        from cli_agent_orchestrator.services import managed_launch_v2 as v2

        def _boom(_terminal_id):
            raise RuntimeError("projection unavailable")

        monkeypatch.setattr(projection, "project_terminal", _boom)

        published = v2._published_terminal_facts("whatever")

        assert set(published) == set(v2.PUBLISHED_TERMINAL_FIELDS)
        assert published["lifecycle_state"] is None


class TestVerificationOutcomesAreDistinguished:
    """Absent, unreadable and dead are three answers, not one.

    Collapsing them is what turned "we could not look" into "the worker is
    gone" — a durable demotion, on no evidence.
    """

    def test_a_backend_that_cannot_answer_leaves_the_name_path_alone(
        self, isolated_memory_db, monkeypatch
    ):
        """A double answers every attribute with a stand-in object.

        Treating that as a declared capability would enforce the boundary
        against evidence the double never gathered, so the check is
        ``is True`` and anything else opts out.
        """

        class Undeclared:
            supports_pane_identity = "yes, obviously"

        monkeypatch.setattr(terminal_service, "get_backend", lambda: Undeclared())
        database.create_terminal(
            terminal_id="aa00aa00",
            tmux_session="cao-up",
            tmux_window="worker",
            provider="claude_code",
            pane_id="%10",
            window_id="@10",
            session_id="$1",
            pane_pid=4242,
            server_socket_path=SOCKET,
        )

        target = terminal_service.verified_pane_target(
            "aa00aa00", database.get_terminal_metadata("aa00aa00"), operation="control input"
        )

        assert target is None

    def test_an_unreadable_pane_is_unknown_liveness_not_dead(self, isolated_memory_db, backend):
        """The server could not be asked. That is not evidence either way."""
        fake = backend({})
        fake._panes = {}
        monkey = {"outcome": "unreadable", "pane_id": "%10"}
        fake.observe_pane_identity = lambda pane_id: dict(monkey)
        database.create_terminal(
            terminal_id="bb00bb00",
            tmux_session="cao-up",
            tmux_window="worker",
            provider="claude_code",
            pane_id="%10",
            window_id="@10",
            session_id="$1",
            pane_pid=4242,
            server_socket_path=SOCKET,
        )

        with pytest.raises(terminal_service.TerminalIdentityMismatchError, match="could not be"):
            terminal_service.verified_pane_target(
                "bb00bb00",
                database.get_terminal_metadata("bb00bb00"),
                operation="control input",
            )

        row = database.get_terminal_metadata("bb00bb00")
        assert row["lifecycle_state"] == terminal_service.LIFECYCLE_UNKNOWN_LIVENESS
        # Not reaped: an unreadable worker may be perfectly alive.
        assert row["lifecycle_state"] != terminal_service.LIFECYCLE_DEAD

    def test_a_dead_pane_is_reported_as_dead(self, isolated_memory_db, backend):
        backend({"%10": _pane(dead="1")})
        database.create_terminal(
            terminal_id="cc00cc00",
            tmux_session="cao-up",
            tmux_window="worker",
            provider="claude_code",
            pane_id="%10",
            window_id="@10",
            session_id="$1",
            pane_pid=4242,
            server_socket_path=SOCKET,
        )

        with pytest.raises(terminal_service.TerminalIdentityMismatchError, match="is dead"):
            terminal_service.verified_pane_target(
                "cc00cc00",
                database.get_terminal_metadata("cc00cc00"),
                operation="control input",
            )
        assert (
            database.get_terminal_metadata("cc00cc00")["lifecycle_state"]
            == terminal_service.LIFECYCLE_DEAD
        )

    def test_a_renamed_window_is_still_the_right_worker(self, isolated_memory_db, backend):
        """A name is a label. Demoting on one would reap live terminals.

        The verified names are the ones the pane answers to *now*, and the
        row's cached labels are refreshed to match — not re-pointed, since
        the identity was just proven unchanged.
        """
        backend({"%10": _pane(window_name="renamed-itself")})
        database.create_terminal(
            terminal_id="dd00dd00",
            tmux_session="cao-up",
            tmux_window="worker",
            provider="claude_code",
            pane_id="%10",
            window_id="@10",
            session_id="$1",
            pane_pid=4242,
            server_socket_path=SOCKET,
        )

        target = terminal_service.verified_pane_target(
            "dd00dd00", database.get_terminal_metadata("dd00dd00"), operation="control input"
        )

        assert target.window_name == "renamed-itself"
        assert database.get_terminal_metadata("dd00dd00")["tmux_window"] == "renamed-itself"
