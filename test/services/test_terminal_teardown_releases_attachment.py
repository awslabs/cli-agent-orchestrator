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
    the wait would only add latency — except in the live-owner case, where
    it would add the full grace period to every run.
    """
    monkeypatch.setattr(recovery, "TEARDOWN_GRACE_SECONDS", 0.0)


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

    def test_teardown_resolves_the_claim_before_deleting_the_row(self):
        """Ordering is the contract, not an accident.

        The claim must be resolved while the terminal's generation claim is
        still held, and after the window kill — the first moment the owning
        process can be observed absent.
        """
        import inspect

        from cli_agent_orchestrator.services import terminal_service

        source = inspect.getsource(terminal_service._delete_terminal_claimed)
        kill = source.index("kill_window")
        release = source.index("release_owned_by_terminal")
        row_delete = source.index("db_delete_terminal_if_generation")
        assert kill < release < row_delete
