"""The managed cleanup tears down the exact generation before it proves ``cleaned``.

A finalized ``negative`` native generation used to leave its pane and
provider process behind: the cleanup removed only the v2 terminal metadata
row and returned a durable ``cleaned`` proof, so a pane survived invisible
to the API and dashboard. ``cleaned`` now means the exact generation's
pane, provider process, fork-owned resources, and terminal row are all gone
— the generation/session-bound teardown runs and is verified before the
proof is persisted.

The exact teardown (``terminal_service.delete_terminal``) is stubbed here
and observed through the identity it is called with; the contract under
test is the cleanup verb's use of it, not tmux itself.
"""

from __future__ import annotations

import hashlib
import subprocess
import uuid

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.managed_launch_v2 import (
    PROTOCOL_VERSION_V2,
    ManagedLaunchV2CleanupRequest,
    ManagedLaunchV2NegativeRequest,
    ManagedLaunchV2ReserveRequest,
)
from cli_agent_orchestrator.services import managed_launch_v2 as v2
from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.services.managed_launch import (
    ManagedLaunchConflict,
    ManagedLaunchUnavailable,
)

TEARDOWN = "cli_agent_orchestrator.services.terminal_service.delete_terminal"


@pytest.fixture
def _companion(tmp_path, monkeypatch):
    monkeypatch.setattr(v2, "COMPANION_DIR", tmp_path / "companion")


@pytest.fixture
def worktree(tmp_path):
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


@pytest.fixture
def finalized(worktree, tmp_path, _companion, isolated_memory_db):
    """A real reservation driven to ``negative`` with its terminal row present."""
    executable = tmp_path / "fake-kimi"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    record, _created = v2.reserve(
        ManagedLaunchV2ReserveRequest(
            protocol_version=PROTOCOL_VERSION_V2,
            reservation_id=str(uuid.uuid4()),
            session_name="cao-chess-shakedown",
            provider="kimi_cli",
            agent_profile="reviewer",
            caller_id="deadbeef",
            working_directory=str(worktree),
            expected_model="kimi-code/kimi-for-coding",
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
    database.create_terminal_v2(
        terminal_id=record["terminal_id"],
        tmux_session="cao-chess-shakedown",
        tmux_window=v2.managed_window_name(record["terminal_id"], record["generation"]),
        provider="kimi_cli",
        generation=record["generation"],
        pane_id="%30",
        window_id="@30",
        server_socket_path="/private/tmp/tmux-501/default",
        session_id="$7",
        pane_pid=54321,
    )
    with database.SessionLocal() as db:
        db.query(database.ManagedLaunchV2ReservationModel).filter(
            database.ManagedLaunchV2ReservationModel.reservation_id == record["reservation_id"]
        ).update({"state": "launching"}, synchronize_session=False)
        db.commit()
    v2.finalize_negative(
        record["reservation_id"],
        ManagedLaunchV2NegativeRequest(
            protocol_version=PROTOCOL_VERSION_V2,
            finalize_id=str(uuid.uuid4()),
            terminal_id=record["terminal_id"],
            generation=record["generation"],
            obligation_generation=record["obligation_generation"],
            reason="launch never reached its bind",
        ),
    )
    return record


def _cleanup_request(record, cleanup_id: str) -> ManagedLaunchV2CleanupRequest:
    return ManagedLaunchV2CleanupRequest(
        protocol_version=PROTOCOL_VERSION_V2,
        cleanup_id=cleanup_id,
        terminal_id=record["terminal_id"],
        generation=record["generation"],
        obligation_generation=record["obligation_generation"],
    )


def _patch_teardown(monkeypatch, calls, *, behavior=None):
    """Install an observing teardown stub.

    ``behavior`` selects a failure mode the real teardown can surface:
    ``"mismatch"`` raises the typed generation-mismatch error a replacement
    incarnation produces, ``"survivor"`` raises the ambiguous window-survived
    error, and ``"ambiguous"`` returns False (teardown could not confirm
    removal). The default confirms removal like a successful teardown.
    """

    def _delete_terminal(
        terminal_id, *, registry=None, expected_generation=None, expected_session=None, **_
    ):
        calls.append(
            {
                "terminal_id": terminal_id,
                "expected_generation": expected_generation,
                "expected_session": expected_session,
                "registry": registry,
            }
        )
        if behavior == "mismatch":
            raise terminal_service.TerminalGenerationMismatchError(
                f"terminal {terminal_id} generation mismatch"
            )
        if behavior == "survivor":
            raise RuntimeError(f"managed terminal window survived cleanup: {terminal_id}")
        if behavior == "ambiguous":
            return False
        return True

    monkeypatch.setattr(TEARDOWN, _delete_terminal)
    return calls


class TestLiveGenerationIsTornDownBeforeCleaned:
    def test_calls_exact_teardown_with_generation_and_session(self, finalized, monkeypatch):
        calls = _patch_teardown(monkeypatch, [])

        v2.cleanup(finalized["reservation_id"], _cleanup_request(finalized, str(uuid.uuid4())))

        assert len(calls) == 1
        call = calls[0]
        assert call["terminal_id"] == finalized["terminal_id"]
        assert call["expected_generation"] == finalized["generation"]
        # The session is the fork-owned reservation identity, so a foreign
        # session cannot reach this generation's teardown.
        assert call["expected_session"] == "cao-chess-shakedown"

    def test_forwards_the_live_registry(self, finalized, monkeypatch):
        calls = _patch_teardown(monkeypatch, [])
        registry = object()

        v2.cleanup(
            finalized["reservation_id"],
            _cleanup_request(finalized, str(uuid.uuid4())),
            registry=registry,
        )

        assert calls[0]["registry"] is registry

    def test_leaves_no_terminal_row_and_returns_cleaned(self, finalized, monkeypatch):
        _patch_teardown(monkeypatch, [])

        out = v2.cleanup(
            finalized["reservation_id"], _cleanup_request(finalized, str(uuid.uuid4()))
        )

        # No survivor: the teardown ran and the v2 terminal row is gone.
        assert database.get_terminal_metadata_v2(finalized["terminal_id"]) is None
        assert out["state"] == "cleaned"
        assert out["cleanup"]["terminal_record_removed"] is True


class TestRowAlreadyRemovedIsStillTornDown:
    def test_a_row_removed_while_its_window_remains_is_still_safely_torn_down(
        self, finalized, monkeypatch
    ):
        """A crash can leave the row gone and the pane alive.

        The managed window name embeds the immutable generation, so the
        teardown is rerun against the same identity. Cleanup must still
        complete and prove removal rather than strand the live pane.
        """
        calls = _patch_teardown(monkeypatch, [])
        # The row is already gone, but (in production) the pane/window remain.
        database.delete_terminal_v2(finalized["terminal_id"])

        out = v2.cleanup(
            finalized["reservation_id"], _cleanup_request(finalized, str(uuid.uuid4()))
        )

        assert len(calls) == 1
        assert calls[0]["expected_generation"] == finalized["generation"]
        assert out["state"] == "cleaned"
        assert out["cleanup"]["terminal_record_removed"] is True


class TestTeardownRefusals:
    def test_a_replacement_incarnation_is_a_conflict_with_no_proof(self, finalized, monkeypatch):
        calls = _patch_teardown(monkeypatch, [], behavior="mismatch")

        with pytest.raises(ManagedLaunchConflict):
            v2.cleanup(finalized["reservation_id"], _cleanup_request(finalized, str(uuid.uuid4())))

        # Zero mutations: the generation stays recoverable in ``negative``
        # with no cleanup proof, and the survivor row it refused to touch.
        assert len(calls) == 1
        after = v2.get(finalized["reservation_id"])
        assert after["state"] == "negative"
        assert after["durable_state"] == "negative"
        assert after["cleanup"] is None
        assert database.get_terminal_metadata_v2(finalized["terminal_id"]) is not None

    def test_a_surviving_window_is_an_error_with_no_proof(self, finalized, monkeypatch):
        _patch_teardown(monkeypatch, [], behavior="survivor")

        with pytest.raises(ManagedLaunchUnavailable):
            v2.cleanup(finalized["reservation_id"], _cleanup_request(finalized, str(uuid.uuid4())))

        after = v2.get(finalized["reservation_id"])
        assert after["state"] == "negative"
        assert after["cleanup"] is None

    def test_an_ambiguous_teardown_is_an_error_with_no_proof(self, finalized, monkeypatch):
        _patch_teardown(monkeypatch, [], behavior="ambiguous")

        with pytest.raises(ManagedLaunchUnavailable):
            v2.cleanup(finalized["reservation_id"], _cleanup_request(finalized, str(uuid.uuid4())))

        after = v2.get(finalized["reservation_id"])
        assert after["state"] == "negative"
        assert after["cleanup"] is None


class TestReplayPerformsNoSecondTeardown:
    def test_same_cleanup_id_replays_the_proof_without_a_second_teardown(
        self, finalized, monkeypatch
    ):
        calls = _patch_teardown(monkeypatch, [])
        cleanup_id = str(uuid.uuid4())

        first = v2.cleanup(finalized["reservation_id"], _cleanup_request(finalized, cleanup_id))
        replay = v2.cleanup(finalized["reservation_id"], _cleanup_request(finalized, cleanup_id))

        # The replay returns the byte-identical stored proof and never
        # reaches teardown a second time.
        assert replay["cleanup"] == first["cleanup"]
        assert len(calls) == 1
