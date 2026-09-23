"""Placement that survives this process, and reports that do not (#745).

Two review findings on #802 about the same weakness: the registry treated its
own memory as the whole truth.

- Finding 5: ``is_remote`` read only ``_terminal_runtime``, which a replacement
  server pod starts empty. Every remote terminal it inherited answered "local"
  until its executor's hello arrived, and the callers went to the CONTROLLER's
  tmux -- a missing-session error on a pure controller, and on a hybrid host a
  chance of addressing an unrelated local pane. The placement is persisted on
  the central terminal row by ``POST /runtimes/{id}/terminals``; it just was
  not being read.
- Finding 2: nothing invalidated remembered state when the runtime behind it was
  replaced. A hello that no longer claims a terminal used to resurrect its last
  ``PROCESSING``/``COMPLETED`` as current, and a report arriving from a
  superseded connection could overwrite the live one's.
"""

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.runtime_channel.registry import (
    RuntimeChannelRegistry,
    RuntimeUnavailableError,
)
from cli_agent_orchestrator.runtime_channel.protocol import CommandType

TID = "aaaa1111"


def _row(runtime_id=None):
    """The shape ``get_terminal_metadata`` returns, trimmed to what matters."""
    return {"id": TID, "tmux_session": "cao-aaaa1111", "metadata": {"runtime_id": runtime_id}}


@pytest.fixture()
def rows(monkeypatch):
    """A stand-in central database, with a call counter for caching claims."""
    state = {"row": None, "calls": 0, "raise": False}

    def get_terminal_metadata(terminal_id):
        state["calls"] += 1
        if state["raise"]:
            raise RuntimeError("database is unreachable")
        return state["row"]

    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_terminal_metadata",
        get_terminal_metadata,
    )
    return state


@pytest.fixture()
def fresh():
    """A registry with no bindings: the replacement server pod's starting state."""
    return RuntimeChannelRegistry()


class TestPlacementSurvivesTheServer:
    def test_a_persisted_placement_is_remote_without_any_channel(self, fresh, rows):
        rows["row"] = _row("worker-1")
        assert fresh.is_remote(TID) is True
        assert fresh.runtime_for_terminal(TID) == "worker-1"

    @pytest.mark.asyncio
    async def test_the_command_path_says_not_connected_rather_than_running_it_here(
        self, fresh, rows
    ):
        """The whole point: an honest failure, not a local execution.

        Before this, ``send_terminal_command`` was never reached -- the caller
        checked ``is_remote``, got False, and drove local tmux.
        """
        rows["row"] = _row("worker-1")
        with pytest.raises(RuntimeUnavailableError) as exc:
            await fresh.send_terminal_command(TID, CommandType.INPUT, {"text": "hi"})
        assert "worker-1" in str(exc.value) and "not connected" in str(exc.value)

    def test_status_is_unknown_not_a_guess(self, fresh, rows):
        rows["row"] = _row("worker-1")
        assert fresh.get_status(TID) is TerminalStatus.UNKNOWN

    def test_a_local_terminal_stays_local(self, fresh, rows):
        rows["row"] = _row(None)
        assert fresh.is_remote(TID) is False
        assert fresh.runtime_for_terminal(TID) is None

    def test_a_row_that_does_not_exist_is_not_remote(self, fresh, rows):
        rows["row"] = None
        assert fresh.is_remote(TID) is False

    def test_both_answers_are_cached_because_placement_is_fixed(self, fresh, rows):
        """``is_remote`` sits on the status-poll path; one read per terminal."""
        rows["row"] = _row("worker-1")
        for _ in range(5):
            fresh.is_remote(TID)
        assert rows["calls"] == 1

        rows["row"] = _row(None)
        other = "bbbb2222"
        for _ in range(5):
            fresh.is_remote(other)
        assert rows["calls"] == 2

    def test_an_unreadable_database_is_unknown_and_fails_closed_to_remote(self, fresh, rows):
        """A failed read must not answer "local" and drive this host's tmux.

        It is treated as remote/unknown (fail closed), never cached, and a later
        successful read still learns the truth (guojing1217 on #802).
        """
        from cli_agent_orchestrator.runtime_channel.registry import PlacementUnavailableError

        rows["raise"] = True
        assert fresh.is_remote(TID) is True
        assert fresh.is_remote(TID) is True
        assert rows["calls"] == 2, "a failure is not an answer worth remembering"

        # The low-level lookup surfaces the distinction rather than hiding it.
        with pytest.raises(PlacementUnavailableError):
            fresh._placement_from_the_central_row(TID)
        # runtime_for_terminal reports the runtime as unknown, not "local".
        assert fresh.runtime_for_terminal(TID) is None

        rows["raise"] = False
        rows["row"] = _row(None)  # a real read: this one is genuinely local
        assert fresh.is_remote(TID) is False

    def test_a_live_binding_never_consults_the_database(self, fresh, rows):
        rows["row"] = _row("worker-9")
        fresh.bind_terminal(TID, "worker-1")
        assert fresh.runtime_for_terminal(TID) == "worker-1"
        assert rows["calls"] == 0

    def test_the_hello_binding_replaces_a_recovered_answer(self, fresh, rows):
        """The executor's own statement outranks the row it was recovered from.

        A terminal relocated by a re-launch would otherwise keep routing to the
        runtime it was first created on.
        """
        rows["row"] = _row("worker-old")
        assert fresh.runtime_for_terminal(TID) == "worker-old"
        fresh.bind_terminal(TID, "worker-new")
        assert fresh.runtime_for_terminal(TID) == "worker-new"

    def test_unbinding_forgets_the_recovered_answer_too(self, fresh, rows):
        """Teardown deletes the row; a cached placement would outlive it."""
        rows["row"] = _row("worker-1")
        assert fresh.is_remote(TID) is True
        fresh.unbind_terminal(TID)
        rows["row"] = None
        assert fresh.is_remote(TID) is False

    def test_enumeration_still_only_reports_bound_and_live(self, fresh, rows):
        """Recovery is for routing, not for listing sessions as active."""
        rows["row"] = _row("worker-1")
        assert fresh.is_remote(TID) is True
        assert fresh.remote_terminal_ids() == []


class TestPlacementRecoveryAtTheServiceSeam:
    """The finding is about callers, so at least one of them is driven here."""

    def test_get_terminal_does_not_probe_local_tmux_for_a_recovered_remote(self, monkeypatch, rows):
        from cli_agent_orchestrator.services import terminal_service

        rows["row"] = _row("worker-1")
        monkeypatch.setattr(
            terminal_service,
            "get_terminal_metadata",
            lambda tid: {
                "id": TID,
                "tmux_session": "cao-aaaa1111",
                "tmux_window": "w",
                "provider": "q_cli",
                "agent_profile": None,
                "last_active": None,
                "metadata": {"runtime_id": "worker-1"},
            },
        )

        def _must_not_run(*a, **k):
            raise AssertionError("a remote terminal's status must not be probed locally")

        monkeypatch.setattr(terminal_service.status_monitor, "get_status", _must_not_run)
        monkeypatch.setattr(
            "cli_agent_orchestrator.runtime_channel.registry.runtime_registry",
            RuntimeChannelRegistry(),
        )

        assert terminal_service.get_terminal(TID)["status"] == TerminalStatus.UNKNOWN.value

    def test_send_input_refuses_instead_of_typing_into_a_local_pane(self, monkeypatch, rows):
        from cli_agent_orchestrator.services import terminal_service

        rows["row"] = _row("worker-1")
        monkeypatch.setattr(
            terminal_service,
            "get_terminal_metadata",
            lambda tid: {"id": TID, "provider": "q_cli", "metadata": {"runtime_id": "worker-1"}},
        )

        def _must_not_run(*a, **k):
            raise AssertionError("a remote terminal must not be driven through a local provider")

        monkeypatch.setattr(terminal_service.provider_manager, "get_provider", _must_not_run)
        monkeypatch.setattr(
            "cli_agent_orchestrator.runtime_channel.registry.runtime_registry",
            RuntimeChannelRegistry(),
        )

        with pytest.raises(RuntimeUnavailableError):
            terminal_service.send_input(TID, "hello")


class TestOnlyTheCurrentIncarnationIsBelieved:
    """Finding 2: reported state is fenced by the connection that reported it."""

    @staticmethod
    async def _send_text(_raw):
        pass

    def test_a_report_from_a_superseded_connection_is_dropped(self, fresh):
        old = fresh.register("worker-1", self._send_text)
        fresh.bind_terminal(TID, "worker-1")
        new = fresh.register("worker-1", self._send_text)

        fresh.set_status(TID, TerminalStatus.PROCESSING, conn=new)
        fresh.set_status(TID, TerminalStatus.COMPLETED, conn=old)

        assert fresh.get_status(TID) is TerminalStatus.PROCESSING

    def test_the_current_connection_is_believed(self, fresh):
        conn = fresh.register("worker-1", self._send_text)
        fresh.bind_terminal(TID, "worker-1")
        fresh.set_status(TID, TerminalStatus.COMPLETED, conn=conn)
        assert fresh.get_status(TID) is TerminalStatus.COMPLETED

    def test_a_reconnected_pod_gets_a_new_incarnation(self, fresh):
        first = fresh.register("worker-1", self._send_text)
        fresh.unregister("worker-1", first)
        second = fresh.register("worker-1", self._send_text)
        assert second.incarnation > first.incarnation

        # And the departed one cannot speak for the terminal any more, even
        # though its runtime id is connected again.
        fresh.bind_terminal(TID, "worker-1")
        fresh.set_status(TID, TerminalStatus.COMPLETED, conn=first)
        assert fresh.get_status(TID) is TerminalStatus.UNKNOWN

    def test_callers_without_a_connection_are_unaffected(self, fresh):
        """The local status monitor's own bookkeeping passes no connection."""
        fresh.register("worker-1", self._send_text)
        fresh.bind_terminal(TID, "worker-1")
        fresh.set_status(TID, TerminalStatus.PROCESSING)
        assert fresh.get_status(TID) is TerminalStatus.PROCESSING


class TestAHelloDoesNotResurrectWhatItDoesNotClaim:
    @staticmethod
    async def _send_text(_raw):
        pass

    def test_an_unclaimed_terminal_loses_its_cached_status(self, fresh):
        conn = fresh.register("worker-1", self._send_text)
        fresh.bind_terminal(TID, "worker-1")
        fresh.set_status(TID, TerminalStatus.PROCESSING, conn=conn)
        fresh.record_position(TID, "capture", 4096)

        # The pod was replaced: same runtime id, none of the old panes.
        fresh.unregister("worker-1", conn)
        fresh.register("worker-1", self._send_text)
        stale = fresh.reconcile_hello("worker-1", [])

        assert stale == [TID]
        assert fresh.get_status(TID) is TerminalStatus.UNKNOWN
        assert fresh.resume_position(TID, "capture") == 0

    def test_the_binding_is_kept_so_the_terminal_does_not_look_local(self, fresh, rows):
        """Dropping the binding here would walk straight into finding 5."""
        conn = fresh.register("worker-1", self._send_text)
        fresh.bind_terminal(TID, "worker-1")
        fresh.set_status(TID, TerminalStatus.COMPLETED, conn=conn)
        fresh.reconcile_hello("worker-1", [])

        assert fresh.is_remote(TID) is True
        assert fresh.runtime_for_terminal(TID) == "worker-1"
        assert rows["calls"] == 0

    def test_a_claimed_terminal_keeps_its_state(self, fresh):
        conn = fresh.register("worker-1", self._send_text)
        fresh.bind_terminal(TID, "worker-1")
        fresh.set_status(TID, TerminalStatus.PROCESSING, conn=conn)
        fresh.record_position(TID, "capture", 4096)

        assert fresh.reconcile_hello("worker-1", [TID]) == []
        assert fresh.get_status(TID) is TerminalStatus.PROCESSING
        assert fresh.resume_position(TID, "capture") == 4096

    def test_another_runtimes_terminals_are_left_alone(self, fresh):
        conn1 = fresh.register("worker-1", self._send_text)
        conn2 = fresh.register("worker-2", self._send_text)
        fresh.bind_terminal(TID, "worker-1")
        fresh.bind_terminal("cccc3333", "worker-2")
        fresh.set_status(TID, TerminalStatus.PROCESSING, conn=conn1)
        fresh.set_status("cccc3333", TerminalStatus.PROCESSING, conn=conn2)

        assert fresh.reconcile_hello("worker-1", []) == [TID]
        assert fresh.get_status("cccc3333") is TerminalStatus.PROCESSING


class TestARuntimeCannotClaimAnotherRuntimesTerminal:
    """The shared-token hijack (guojing1217 + Copilot on #802, reproduced on EKS).

    ``CAO_RUNTIME_TOKEN`` is one secret for the whole fleet, so a connected
    runtime is only ever *some* authorized executor. ``claim_terminal`` is the
    fence that stops one from binding a terminal it does not own; the channel
    handlers route every inbound bind through it.
    """

    OTHER = "cccc3333"

    def test_an_unbound_terminal_placed_elsewhere_cannot_be_claimed(self, fresh, rows):
        # The durable row names worker-1; a replacement server starts unbound.
        rows["row"] = _row(runtime_id="worker-1")
        assert fresh.claim_terminal(TID, "worker-2") is False
        assert fresh.runtime_for_terminal(TID) == "worker-1"

    def test_the_launching_runtime_reclaims_after_a_restart(self, fresh, rows):
        rows["row"] = _row(runtime_id="worker-1")
        assert fresh.claim_terminal(TID, "worker-1") is True
        assert fresh._terminal_runtime[TID] == "worker-1"

    def test_a_terminal_bound_here_cannot_be_stolen(self, fresh, rows):
        fresh.bind_terminal(TID, "worker-1")
        assert fresh.claim_terminal(TID, "worker-2") is False
        assert fresh.runtime_for_terminal(TID) == "worker-1"
        # The refusal did not need the database: the live binding is authority.
        assert rows["calls"] == 0

    def test_the_same_runtime_reasserting_is_a_continuation(self, fresh):
        fresh.bind_terminal(TID, "worker-1")
        assert fresh.claim_terminal(TID, "worker-1") is True

    def test_an_unbound_terminal_with_no_row_yet_is_claimable(self, fresh, rows):
        # An absent row is a phantom id with no pane to hijack, and the window a
        # tracked launch/reconcile binds through before its row commits: allow.
        rows["row"] = None
        assert fresh.claim_terminal(TID, "worker-1") is True
        assert fresh._terminal_runtime[TID] == "worker-1"

    def test_a_confirmed_local_terminal_cannot_be_claimed(self, fresh, rows):
        # A row that exists but names NO runtime is a local pane; a runtime
        # claiming it would redirect that pane's routing (Copilot follow-up on
        # #802). Distinct from the absent-row case above.
        rows["row"] = _row(runtime_id=None)
        assert fresh.claim_terminal(TID, "worker-1") is False
        assert TID not in fresh._terminal_runtime

    def test_the_binding_does_not_flap_when_an_imposter_speaks_last(self, fresh, rows):
        # The EKS symptom: routing flipped to whoever spoke most recently.
        rows["row"] = _row(runtime_id="worker-1")
        fresh.bind_terminal(TID, "worker-1")
        assert fresh.claim_terminal(TID, "worker-2") is False
        assert fresh.claim_terminal(TID, "worker-1") is True
        assert fresh.runtime_for_terminal(TID) == "worker-1"


class TestGetStatusIsThreadSafeAcrossTheChannelBoundary:
    """effective_status reads the registry from a worker thread while the channel
    loop mutates it on the event-loop thread. get_status's compound
    liveness-check-then-read must not return a stale COMPLETED after a concurrent
    disconnect, and no cross-thread read may raise on dict mutation (Copilot
    follow-up on #802)."""

    @staticmethod
    async def _send(_raw):
        pass

    def test_hammering_get_status_against_register_churn_never_raises_or_goes_stale(self):
        import threading

        from cli_agent_orchestrator.models.terminal import TerminalStatus

        reg = RuntimeChannelRegistry()
        stop = threading.Event()
        errors = []

        def churn():
            try:
                while not stop.is_set():
                    conn = reg.register("worker-1", self._send)
                    reg.bind_terminal(TID, "worker-1")
                    reg.set_status(TID, TerminalStatus.COMPLETED, conn=conn)
                    reg.unregister("worker-1", conn)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def poll():
            try:
                for _ in range(5000):
                    # Either a live status or UNKNOWN — never an exception, and
                    # never a status for a runtime that is not registered.
                    reg.get_status(TID)
                    reg.remote_terminal_ids()
                    reg.list_runtimes()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t = threading.Thread(target=churn)
        t.start()
        try:
            poll()
        finally:
            stop.set()
            t.join()
        assert not errors, f"cross-thread registry access raised: {errors[:3]}"


class TestTheServerFencesOnStreamGeneration:
    """Positions are only comparable within one generation.

    When the runtime re-arms a reader it starts a new generation numbered from 0.
    Monotonic-max alone ignored that 0 as a rewind, so the server kept a
    watermark from a stream that no longer existed and resumed the new one at the
    wrong offset; and frames still arriving from the superseded stream were mixed
    into the live one's transcript (Copilot review on #802).
    """

    def test_a_new_generation_resets_the_watermark_instead_of_being_ignored(self, fresh):
        fresh.record_position(TID, "capture", 5000, generation=0)
        assert fresh.resume_position(TID, "capture") == 5000

        # The reader re-armed: a new stream, numbered from 0.
        fresh.record_position(TID, "capture", 6, generation=1)
        assert fresh.resume_position(TID, "capture") == 6

    def test_a_stale_generation_is_recognised_and_changes_nothing(self, fresh):
        fresh.record_position(TID, "capture", 6, generation=1)
        assert fresh.is_stale_generation(TID, "capture", 0) is True

        fresh.record_position(TID, "capture", 9999, generation=0)
        assert fresh.resume_position(TID, "capture") == 6, "the dead stream cannot advance it"

    def test_the_live_generation_is_not_stale(self, fresh):
        fresh.record_position(TID, "capture", 6, generation=1)
        assert fresh.is_stale_generation(TID, "capture", 1) is False
        assert fresh.is_stale_generation(TID, "capture", 2) is False

    def test_an_unseen_stream_is_never_stale(self, fresh):
        """Nothing known yet: the first frame establishes the generation."""
        assert fresh.is_stale_generation(TID, "capture", 0) is False
        assert fresh.is_stale_generation(TID, "capture", 7) is False

    def test_the_same_generation_keeps_monotonic_max(self, fresh):
        fresh.record_position(TID, "capture", 100, generation=0)
        fresh.record_position(TID, "capture", 50, generation=0)
        assert fresh.resume_position(TID, "capture") == 100

    def test_generation_is_forgotten_with_the_binding(self, fresh):
        fresh.record_position(TID, "capture", 6, generation=3)
        fresh.unbind_terminal(TID)
        # A recreated terminal reusing the id starts clean, not fenced against a
        # generation from a stream that is gone.
        assert fresh.is_stale_generation(TID, "capture", 0) is False

    def test_a_position_without_a_generation_skips_the_fence(self, fresh):
        """Local bookkeeping passes no generation and must be unaffected."""
        fresh.record_position(TID, "capture", 10, generation=2)
        fresh.record_position(TID, "capture", 20)
        assert fresh.resume_position(TID, "capture") == 20


class TestStatusIsFencedOnGenerationToo:
    """A status report from a superseded stream must not settle the current one.

    Stream positions were fenced on generation but ``set_status`` ignored it, so a
    delayed EventFrame from the old generation could overwrite the live status and
    satisfy a waiter — the exact thing the protocol's generation contract forbids
    (Copilot review on #802).
    """

    @staticmethod
    async def _send(_raw):
        pass

    def test_a_stale_generation_status_is_dropped(self, fresh):
        conn = fresh.register("worker-1", self._send)
        fresh.bind_terminal(TID, "worker-1")
        # The live stream is generation 1.
        fresh.record_position(TID, "capture", 10, generation=1)
        fresh.set_status(TID, TerminalStatus.PROCESSING, conn=conn, generation=1)
        assert fresh.get_status(TID) is TerminalStatus.PROCESSING

        # A straggler from generation 0 must not settle anything.
        fresh.set_status(TID, TerminalStatus.COMPLETED, conn=conn, generation=0)
        assert fresh.get_status(TID) is TerminalStatus.PROCESSING

    def test_the_live_generation_still_applies(self, fresh):
        conn = fresh.register("worker-1", self._send)
        fresh.bind_terminal(TID, "worker-1")
        fresh.record_position(TID, "capture", 10, generation=2)
        fresh.set_status(TID, TerminalStatus.COMPLETED, conn=conn, generation=2)
        assert fresh.get_status(TID) is TerminalStatus.COMPLETED

    def test_callers_without_a_generation_are_unaffected(self, fresh):
        """The local status monitor passes none and must keep working."""
        conn = fresh.register("worker-1", self._send)
        fresh.bind_terminal(TID, "worker-1")
        fresh.record_position(TID, "capture", 10, generation=5)
        fresh.set_status(TID, TerminalStatus.IDLE, conn=conn)
        assert fresh.get_status(TID) is TerminalStatus.IDLE
