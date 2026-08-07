"""Terminal teardown closes the provider-session claim it opened.

This file is the regression test whose absence *was* the defect. Every
piece of the machinery already existed — `native_attachment.release` is
implemented, documented and unit-tested — and nothing called it, so every
claim ever taken stayed live and every provider-native session on an
install became permanently unresumable. No test noticed, because no test
asked what teardown does to the claim.

The negatives are the load-bearing half. A teardown that released a claim
whose process is still running would hand that provider session to a
second attacher, and two agents interleaving turns into one transcript is
undetectable downstream — each one's own receipts look consistent.
"""

from __future__ import annotations

import os
import subprocess
import sys
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.services import native_attachment as na
from cli_agent_orchestrator.services import native_attachment_recovery as recovery
from cli_agent_orchestrator.services import native_tui_launch
from cli_agent_orchestrator.services.terminal_service import delete_terminal
from cli_agent_orchestrator.utils.terminal import managed_window_name

PROVIDER = "kimi_cli"
SESSION = "session_326c5026"
TERMINAL = "a1b2c3d4"
GENERATION = "6e3d642e-1f0a-4b7c-9d2e-3a5b7c9d1e2f"
SESSION_NAME = "cao-session"


@pytest.fixture(autouse=True)
def _db(isolated_memory_db):
    return isolated_memory_db


@pytest.fixture(autouse=True)
def _no_teardown_grace(monkeypatch):
    """Teardown waits for a just-killed provider to leave the process table.

    These tests supply a pid that is already gone or already permanent, so
    the wait only adds latency — except in the live-owner cases, where it
    would add the full grace period to every run.

    This fixture used to be a no-op: `release_owned_by_terminal` bound the
    module constant as a default argument, which captures its value once at
    import, so setting it here changed nothing and the suite paid two
    seconds per live-owner test while believing it did not.
    """
    monkeypatch.setattr(recovery, "TEARDOWN_GRACE_SECONDS", 0.0)


def test_the_grace_fixture_is_not_a_no_op(monkeypatch):
    """Pin the thing that was silently broken: a default argument captured
    at import cannot be overridden by setting the constant."""
    observed = []
    monkeypatch.setattr(recovery, "TEARDOWN_GRACE_SECONDS", 7.5)
    monkeypatch.setattr(
        recovery,
        "_observe_until_gone",
        lambda record, *, grace_seconds: observed.append(grace_seconds)
        or recovery.observe_owner(record),
    )
    _attach(pid=_reaped_pid())
    recovery.release_owned_by_terminal(TERMINAL)
    assert observed == [7.5]


def _reaped_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


def _attach(
    *,
    pid: int,
    session: str = SESSION,
    terminal_id: str = TERMINAL,
    generation: str = GENERATION,
    marker: str = "Fri Aug  7 12:51:19 2026",
) -> dict:
    owner = {
        "terminal_id": terminal_id,
        "generation": generation,
        "execution_mode": "native_tui",
    }
    na.declare(
        provider=PROVIDER,
        native_session_id=session,
        pane_id="%12",
        intent=na.acquire_intent(
            acquisition_method=na.ACQUISITION_ACP_BOOTSTRAP,
            acquisition_receipt={"kind": "kimi-acp-session-new", "session_id": session},
            admits_only_new_instructions=True,
            replays_task_bytes=False,
            bootstrap_sent_no_turn=True,
            bootstrap_detached_before_launch=True,
        ),
        **owner,
    )
    na.mark_starting(provider=PROVIDER, native_session_id=session, **owner)
    return na.mark_attached(
        provider=PROVIDER,
        native_session_id=session,
        process_identity=na.process_identity(pid=pid, start_marker=marker),
        **owner,
    )


def _bare_teardown(**overrides):
    """The id-only teardown form, as `conduct collect` and the orphan sweep use."""
    patches = {
        "get_terminal_metadata": {
            "tmux_session": SESSION_NAME,
            "tmux_window": "developer-abcd",
        },
        "db_delete_terminal": True,
    }
    patches.update(overrides)
    ts = "cli_agent_orchestrator.services.terminal_service"
    with (
        patch(f"{ts}.status_monitor"),
        patch(f"{ts}.fifo_manager"),
        patch(f"{ts}.provider_manager"),
        patch("cli_agent_orchestrator.backends.registry._backend"),
        patch(f"{ts}.get_terminal_metadata", return_value=patches["get_terminal_metadata"]),
        patch(f"{ts}.db_delete_terminal", return_value=patches["db_delete_terminal"]),
    ):
        return delete_terminal(TERMINAL)


class TestTeardownResolvesTheClaim:
    def test_teardown_releases_the_claim_of_a_dead_owner(self):
        """The test whose absence was the bug."""
        _attach(pid=_reaped_pid())
        assert _bare_teardown() is True

        stored = na.get(PROVIDER, SESSION)
        assert stored["state"] == na.DETACHED
        assert stored["release_proof"]["schema"] == na.NO_SURVIVOR_PROOF_SCHEMA
        assert stored["release_proof"]["survivors"] == []

    def test_the_released_session_is_immediately_reclaimable(self):
        """Releasing is only worth doing if the session comes back."""
        _attach(pid=_reaped_pid())
        _bare_teardown()
        _, acquired = na.declare(
            provider=PROVIDER,
            native_session_id=SESSION,
            terminal_id="ffffffff",
            generation="99999999",
            execution_mode="native_tui",
            intent=na.acquire_intent(
                acquisition_method=na.ACQUISITION_RESUME,
                acquisition_receipt={"kind": "resume", "session_id": SESSION},
                admits_only_new_instructions=True,
                replays_task_bytes=False,
            ),
        )
        assert acquired is True

    def test_teardown_holds_a_claim_whose_process_is_still_running(self):
        """The refusal that matters. Two owners on one session is the harm."""
        _attach(pid=os.getpid())
        assert _bare_teardown() is True
        assert na.get(PROVIDER, SESSION)["state"] == na.ATTACHED

    def test_teardown_leaves_another_terminals_claim_alone(self):
        _attach(pid=_reaped_pid(), session="someone-elses", terminal_id="other999")
        _bare_teardown()
        assert na.get(PROVIDER, "someone-elses")["state"] == na.ATTACHED

    def test_teardown_is_idempotent_against_an_already_released_claim(self):
        _attach(pid=_reaped_pid())
        _bare_teardown()
        first = na.get(PROVIDER, SESSION)
        assert _bare_teardown() is True
        assert na.get(PROVIDER, SESSION)["epoch"] == first["epoch"]

    def test_a_failing_release_does_not_abort_the_teardown(self):
        """The window is already killed and the row is about to go.

        Aborting here would leave a half-torn-down terminal, which is worse
        than the claim the step was trying to resolve.
        """
        _attach(pid=_reaped_pid())
        with patch.object(
            recovery,
            "release_owned_by_terminal",
            side_effect=na.NativeAttachmentUnavailable("database is locked"),
        ):
            assert _bare_teardown() is True
        assert na.get(PROVIDER, SESSION)["state"] == na.ATTACHED

    def test_a_bare_teardown_never_freezes_a_survivor(self):
        """An id-only teardown cannot tell a replacement generation apart.

        Freezing on that evidence would take a live, working session out of
        circulation to punish it for existing. Releasing stays safe for the
        ordinary reason: a live owner's pid is alive.
        """
        _attach(pid=os.getpid())
        _bare_teardown()
        assert na.get(PROVIDER, SESSION)["state"] == na.ATTACHED


class TestGenerationScopedTeardown:
    def _teardown(self, generation: str = GENERATION):
        ts = "cli_agent_orchestrator.services.terminal_service"
        metadata = {
            "tmux_session": SESSION_NAME,
            "tmux_window": managed_window_name(TERMINAL, generation),
            "generation": generation,
        }
        backend = patch("cli_agent_orchestrator.backends.registry._backend")
        with (
            patch(f"{ts}.status_monitor"),
            patch(f"{ts}.fifo_manager"),
            patch(f"{ts}.provider_manager"),
            backend as mock_backend,
            patch(f"{ts}.get_terminal_metadata", return_value=metadata),
            patch(f"{ts}.get_terminal_metadata_v2", return_value=None),
            patch(f"{ts}.db_delete_terminal_if_generation", return_value=True),
        ):
            mock_backend.window_exists.return_value = False
            return delete_terminal(
                TERMINAL,
                expected_generation=generation,
                expected_session=SESSION_NAME,
            )

    def test_teardown_releases_only_the_generation_it_claimed(self):
        _attach(pid=_reaped_pid(), session="gen-a", generation=GENERATION)
        _attach(
            pid=_reaped_pid(), session="gen-b", generation="0f1e2d3c-4b5a-4697-8899-aabbccddeeff"
        )
        assert self._teardown() is True
        assert na.get(PROVIDER, "gen-a")["state"] == na.DETACHED
        assert na.get(PROVIDER, "gen-b")["state"] == na.ATTACHED

    def test_an_owner_that_outlives_its_own_generation_is_left_attached(self):
        """Not frozen. Freezing was the first design here and it was wrong.

        `mark_ambiguous` is terminal for automation, so freezing would turn
        a state the sweep resolves on its own — the provider dies a second
        later, the next sweep releases it — into one that permanently needs
        a human. It also mislabels the evidence: "frozen" means ownership
        could not be determined, and here it was determined exactly.
        """
        _attach(pid=os.getpid())
        assert self._teardown() is True
        assert na.get(PROVIDER, SESSION)["state"] == na.ATTACHED

    def test_a_later_sweep_releases_what_teardown_had_to_leave(self):
        """End to end, with a real process: the reason leaving it costs nothing.

        Teardown finds the provider still running and holds the claim. The
        provider then exits, and the next sweep releases it — no human, no
        frozen row, no permanent orphan.
        """
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            marker = native_tui_launch._process_field(child.pid, "lstart=")
            assert marker, "could not read the child's start marker"
            _attach(pid=child.pid, marker=marker)

            assert self._teardown() is True
            assert na.get(PROVIDER, SESSION)["state"] == na.ATTACHED
            assert recovery.sweep(apply=True)["counts"].get("released", 0) == 0
        finally:
            child.kill()
            child.wait()

        assert recovery.sweep(apply=True)["counts"]["released"] == 1
        assert na.get(PROVIDER, SESSION)["state"] == na.DETACHED


class TestTheWiringExists:
    def test_some_production_path_calls_release(self):
        """A cheap pin on the thing that was missing for the whole of cond-0209.

        The unit tests for `release` all passed while nothing called it. A
        test that only exercises the function cannot notice that.
        """
        import pathlib

        src = pathlib.Path(__file__).resolve().parents[2] / "src"
        callers = [
            path
            for path in src.rglob("*.py")
            if path.name not in {"native_attachment.py"}
            and "native_attachment.release(" in path.read_text(encoding="utf-8")
        ]
        assert callers, "no production module calls native_attachment.release"

    def test_the_claim_is_resolved_after_the_kill_and_before_the_row_delete(self):
        """Ordering is the contract, and this asserts the executed order.

        The first version of this test compared *textual* offsets in the
        function's source. It passed for the wrong reason: the first
        `kill_window` in the text sits inside a nested recovery-branch
        closure whose position says nothing about execution, so moving the
        release to before the ordinary kill left the assertion green — and
        that mutation silently reverts the commit to the bug it fixes.
        """
        calls: list[str] = []
        ts = "cli_agent_orchestrator.services.terminal_service"
        metadata = {
            "tmux_session": SESSION_NAME,
            "tmux_window": managed_window_name(TERMINAL, GENERATION),
            "generation": GENERATION,
        }
        _attach(pid=_reaped_pid())

        real_release = recovery.release_owned_by_terminal

        def _record_release(*args, **kwargs):
            calls.append("release")
            return real_release(*args, **kwargs)

        def _record_delete(*args, **kwargs):
            calls.append("row-delete")
            return True

        with (
            patch(f"{ts}.status_monitor"),
            patch(f"{ts}.fifo_manager"),
            patch(f"{ts}.provider_manager"),
            patch("cli_agent_orchestrator.backends.registry._backend") as backend,
            patch(f"{ts}.get_terminal_metadata", return_value=metadata),
            patch(f"{ts}.get_terminal_metadata_v2", return_value=None),
            patch(f"{ts}.db_delete_terminal_if_generation", side_effect=_record_delete),
            patch.object(recovery, "release_owned_by_terminal", side_effect=_record_release),
        ):
            backend.window_exists.return_value = False
            backend.kill_window.side_effect = lambda *a, **k: calls.append("kill")
            delete_terminal(TERMINAL, expected_generation=GENERATION, expected_session=SESSION_NAME)

        assert calls == ["kill", "release", "row-delete"]

    def test_an_owner_that_dies_at_the_window_kill_is_released(self):
        """The whole reason the release runs after the kill.

        Every other test in this file uses a process whose liveness does not
        change during teardown — already reaped, or this interpreter. Neither
        exercises the ordering at all: a release placed *before* the kill
        would pass both.
        """
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            marker = native_tui_launch._process_field(child.pid, "lstart=")
            assert marker
            _attach(pid=child.pid, marker=marker)

            ts = "cli_agent_orchestrator.services.terminal_service"
            metadata = {
                "tmux_session": SESSION_NAME,
                "tmux_window": managed_window_name(TERMINAL, GENERATION),
                "generation": GENERATION,
            }

            def _kill_the_provider(*_args, **_kwargs):
                child.kill()
                child.wait()

            with (
                patch(f"{ts}.status_monitor"),
                patch(f"{ts}.fifo_manager"),
                patch(f"{ts}.provider_manager"),
                patch("cli_agent_orchestrator.backends.registry._backend") as backend,
                patch(f"{ts}.get_terminal_metadata", return_value=metadata),
                patch(f"{ts}.get_terminal_metadata_v2", return_value=None),
                patch(f"{ts}.db_delete_terminal_if_generation", return_value=True),
            ):
                backend.window_exists.return_value = False
                backend.kill_window.side_effect = _kill_the_provider
                delete_terminal(
                    TERMINAL, expected_generation=GENERATION, expected_session=SESSION_NAME
                )
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()

        assert na.get(PROVIDER, SESSION)["state"] == na.DETACHED


class TestTeardownDoesNotUndoADeliberateFreeze:
    """`managed_launch_v2` freezes a session whose route proof failed.

    Its contract is that the claim is frozen "so a later launch cannot
    reuse an uncertain session". Teardown now resolves the claims it ends,
    and the pane it just killed leaves no survivor — so with the original
    ordering the release succeeded and the freeze that was the whole point
    of the function found a detached row with no owner to freeze. The
    uncertain session became freely re-acquirable.

    The one existing test of that function stubs `delete_terminal`, so it
    never exercised the function whose behaviour changed. This one does not
    stub it.
    """

    def _teardown_via_route_failure(self):
        import asyncio

        from cli_agent_orchestrator.services import managed_launch_v2 as v2

        ts = "cli_agent_orchestrator.services.terminal_service"
        metadata = {
            "tmux_session": SESSION_NAME,
            "tmux_window": managed_window_name(TERMINAL, GENERATION),
            "generation": GENERATION,
        }
        with (
            patch(f"{ts}.status_monitor"),
            patch(f"{ts}.fifo_manager"),
            patch(f"{ts}.provider_manager"),
            patch("cli_agent_orchestrator.backends.registry._backend") as backend,
            patch(f"{ts}.get_terminal_metadata", return_value=metadata),
            patch(f"{ts}.get_terminal_metadata_v2", return_value=None),
            patch(f"{ts}.db_delete_terminal_if_generation", return_value=True),
        ):
            backend.window_exists.return_value = False
            return asyncio.run(
                v2._teardown_published_native_terminal(
                    record={
                        "terminal_id": TERMINAL,
                        "generation": GENERATION,
                        "session_name": SESSION_NAME,
                        "provider": PROVIDER,
                    },
                    bootstrap={"native_session_id": SESSION},
                    registry=None,
                    reason="glm_route_consumed_marker_missing",
                )
            )

    def test_the_session_stays_frozen_rather_than_being_released(self):
        _attach(pid=_reaped_pid())
        assert self._teardown_via_route_failure() is None

        stored = na.get(PROVIDER, SESSION)
        assert stored["state"] == na.AMBIGUOUS
        assert stored["ambiguity_reason"] == "glm_route_consumed_marker_missing"

    def test_the_uncertain_session_cannot_be_reclaimed(self):
        """The consequence the freeze exists to produce."""
        _attach(pid=_reaped_pid())
        self._teardown_via_route_failure()
        with pytest.raises(na.NativeAttachmentConflict):
            na.declare(
                provider=PROVIDER,
                native_session_id=SESSION,
                terminal_id="ffffffff",
                generation="99999999",
                execution_mode="native_tui",
                intent=na.acquire_intent(
                    acquisition_method=na.ACQUISITION_RESUME,
                    acquisition_receipt={"kind": "resume", "session_id": SESSION},
                    admits_only_new_instructions=True,
                    replays_task_bytes=False,
                ),
            )

    def test_the_ordinary_path_reports_no_cleanup_error(self):
        """A spurious 'freeze failed' clause would reach the operator's
        blocked detail on every route refusal."""
        _attach(pid=_reaped_pid())
        assert self._teardown_via_route_failure() is None
