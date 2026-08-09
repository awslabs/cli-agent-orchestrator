"""Control input reaching a managed native TUI, driven through production.

Every test here builds a **real** reserved, launched and bound native
generation -- real reservation row, real durable binding, real exclusive
attachment -- and then asks the real
``managed_launch.managed_control_identity`` and the real
``control_input_service.deliver_control_input`` what they do with it.

That is the point rather than a style preference.  The deployed
projection returned exactly ``reservation_id``, ``terminal_id``,
``generation``, ``provider``, ``state``, ``controllable`` and
``vintage`` -- and **none** of ``execution_mode``, ``native_session_id``
or ``provider_process_id``.  A fixture that mocks those three describes a
shape production never emitted, so it passes against the broken server
and proves nothing.  Four blockers in this campaign have come from
exactly that gap between a producer and a consumer that were each
individually correct and separately tested.

The provider here is Kimi on purpose.  §24.10.1 deliberately gives
``kimi_cli`` the *no-proof* readiness sibling: it publishes no
provider-authored identity keys, because Kimi's readiness is an observed
attached pane rather than a claim the provider makes about itself.  A
projection sourced from that sibling would therefore find nothing for
every healthy Kimi generation and refuse them all -- so Kimi is the
provider that proves the projection reads the binding and the attachment
instead.
"""

from __future__ import annotations

import hashlib
import subprocess
import time
import uuid
from typing import Any, Mapping, Optional

import pytest

from cli_agent_orchestrator.clients import database as _db
from cli_agent_orchestrator.models.managed_launch_v2 import (
    PROTOCOL_VERSION_V2,
    ManagedLaunchV2BindRequest,
    ManagedLaunchV2ReserveRequest,
)
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import control_input_service as cis
from cli_agent_orchestrator.services import execution_mode as em
from cli_agent_orchestrator.services import kimi_native_bootstrap as boot
from cli_agent_orchestrator.services import kimi_native_control as knc
from cli_agent_orchestrator.services import managed_launch
from cli_agent_orchestrator.services import managed_launch_v2 as v2
from cli_agent_orchestrator.services import managed_provider_bridge as bridge
from cli_agent_orchestrator.services import native_attachment
from cli_agent_orchestrator.services import native_pane_input as npi
from cli_agent_orchestrator.services import native_tui_launch
from cli_agent_orchestrator.services.control_input_journal import (
    STATE_REFUSED,
    ControlInputJournal,
)

PINNED_VERSION_BANNER = "kimi 0.29.0"
SESSION_ID = "session_9f2c41ab"
MODEL = "gpt-5.6-sol"
EFFORT = "xhigh"
PANE_ID = "%7"
WINDOW_ID = "@7"
PANE_PID = 4321
START_MARKER = "Thu Jul 24 10:00:00 2026"
SERVER_SOCKET = "/private/tmp/cao-native.sock"

#: The exact scalar the projection must publish.  A bare pid is the
#: forgeable half on its own -- pids recycle, so a stale one can match an
#: unrelated live process and forge a survivor, or match nothing and
#: forge a no-survivor.
PROVEN_PROCESS_ID = f"{PANE_PID}@{START_MARKER}"


# --------------------------------------------------------------------
# A real native generation
# --------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _companion(tmp_path, monkeypatch):
    # The native binder publishes the exact successor/token record through
    # its module-local root, while the production byte-admission seam resolves
    # the root from ``constants`` at delivery time.  Point both at this test's
    # isolated companion tree so these are real current-v2 identities rather
    # than legacy-looking fixtures with no current fencing token.
    from cli_agent_orchestrator import constants

    companion = tmp_path / "companion"
    monkeypatch.setattr(constants, "COMPANION_DIR", companion)
    monkeypatch.setattr(v2, "COMPANION_DIR", companion)
    monkeypatch.setattr(bridge, "BRIDGE_ROOT", tmp_path / "bridge")
    cis.reset_control_input_journal()


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


def _reserve_request(worktree, tmp_path, **changes):
    executable = tmp_path / "fake-kimi"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    payload = {
        "protocol_version": PROTOCOL_VERSION_V2,
        "reservation_id": str(uuid.uuid4()),
        "session_name": "cao-test",
        "provider": "kimi_cli",
        "agent_profile": "reviewer",
        "caller_id": "deadbeef",
        "working_directory": str(worktree),
        "trusted_project_root": None,
        "expected_model": MODEL,
        "expected_effort": EFFORT,
        "provider_executable": str(executable),
        "provider_executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "obligation_generation": "obgen-7c2e4a1b",
        "task_id": "self-heal-demo-task",
        "run_id": "run-0001",
        "delivery_id": "44444444-4444-4444-8444-444444444444",
        "launch_nonce": "n" * 40,
        "execution_mode": "native_tui",
    }
    payload.update(changes)
    return ManagedLaunchV2ReserveRequest(**payload)


class _FakeAcp:
    """The zero-prompt bootstrap transport: mints an id, sends no turn."""

    def __init__(self) -> None:
        self._options = [
            {"id": "model", "category": "model", "currentValue": "kimi-default"},
            {"id": "thinking", "category": "thought_level", "currentValue": "low"},
        ]

    def request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        if method == "initialize":
            return {"protocolVersion": 1}
        if method == "session/new":
            return {"sessionId": SESSION_ID, "configOptions": self._options}
        if method == "session/set_config_option":
            for option in self._options:
                if option["id"] == params["configId"]:
                    option["currentValue"] = params["value"]
            return {"configOptions": self._options}
        raise AssertionError(f"unexpected bootstrap method {method!r}")

    def terminate(self) -> Mapping[str, Any]:
        return {
            "pid": 4242,
            "exit_status": 0,
            "escalation": [boot.STEP_STDIN_CLOSED],
            "reaped": True,
        }


class _Pane:
    """The live pane the identity check re-reads before every write."""

    def __init__(self, state: "_Native") -> None:
        self._state = state

    def observe(self, *, deadline_monotonic: Optional[float] = None) -> Optional[Mapping[str, Any]]:
        self._state.observed_deadlines.append(deadline_monotonic)
        if self._state.pane_timeout:
            assert deadline_monotonic is not None
            remaining = max(0.0, deadline_monotonic - time.monotonic())
            if remaining:
                time.sleep(remaining)
            raise subprocess.TimeoutExpired(["tmux", "list-panes"], remaining)
        if self._state.pane_gone:
            return None
        if self._state.pane_unreadable:
            raise native_tui_launch.NativeLaunchUnavailable("staged observation failure")
        return {
            "pane_id": self._state.observed_pane_id,
            "pid": self._state.observed_pid,
            "start_marker": self._state.observed_start_marker,
            "argv": ["kimi"],
        }


class _Native:
    """A real native generation, plus the few live seams a unit test owns."""

    def __init__(self) -> None:
        self.terminals: list[dict[str, Any]] = []
        self.pane_gone = False
        self.pane_unreadable = False
        self.pane_timeout = False
        self.observed_deadlines: list[Optional[float]] = []
        self.observed_pane_id = PANE_ID
        self.observed_pid = PANE_PID
        self.observed_start_marker = START_MARKER

    @property
    def launched_argv(self) -> list[str]:
        assert self.terminals, "no pane was ever created"
        return list(self.terminals[-1]["managed_native_command"])


@pytest.fixture
def native(monkeypatch):
    state = _Native()

    async def _create_terminal(**kwargs):
        state.terminals.append(kwargs)
        terminal_id = kwargs["reserved_terminal_id"]
        _db.create_terminal_v2(
            terminal_id,
            kwargs.get("session_name") or "cao-test",
            kwargs.get("window_name") or f"w-{terminal_id}",
            kwargs.get("provider") or "kimi_cli",
            generation=kwargs.get("terminal_generation"),
            pane_id=PANE_ID,
            window_id=WINDOW_ID,
            server_socket_path=SERVER_SOCKET,
            session_id="$1",
            pane_pid=PANE_PID,
        )
        return {"terminal_id": terminal_id}

    def _launch_observe(self):
        return {
            "pane_id": PANE_ID,
            "pid": PANE_PID,
            "start_marker": START_MARKER,
            "argv": state.launched_argv,
            "cwd": self._record["working_directory"],
        }

    monkeypatch.setattr(boot, "StdioAcpBootstrap", lambda **kwargs: _FakeAcp())
    monkeypatch.setattr(bridge, "provider_version_banner", lambda *a, **k: PINNED_VERSION_BANNER)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.create_terminal", _create_terminal
    )
    monkeypatch.setattr(v2._V2NativePane, "observe", _launch_observe)
    monkeypatch.setattr(native_tui_launch, "TmuxNativePane", lambda *a, **k: _Pane(state))
    # The provider's own reading of its own screen. Bind refuses without
    # an observed-idle pane, so without this the receipt would carry
    # input_ready=false and no generation would ever reach ``bound``.
    monkeypatch.setattr(npi, "observe_kimi_turn_state", lambda *a, **k: TerminalStatus.IDLE)
    return state


async def _bound_native(worktree, tmp_path):
    """Reserve, launch and bind one real native generation."""
    record, _ = v2.reserve(_reserve_request(worktree, tmp_path))
    assert record["execution_mode"] == em.NATIVE_TUI
    reservation_id = record["reservation_id"]
    launched = await v2.launch_reserved(reservation_id)
    bound = v2.bind_native(
        reservation_id,
        ManagedLaunchV2BindRequest(
            protocol_version=PROTOCOL_VERSION_V2,
            terminal_id=launched["terminal_id"],
            generation=launched["generation"],
            attempt_id=str(uuid.uuid4()),
        ),
    )
    assert bound["state"] == "bound"
    # Bind is what makes this a real managed-v2 writer: every delivery test
    # below must begin with the exact successor record that its immutable
    # binding names, rather than relying on the old generation-only path.
    from cli_agent_orchestrator.services import heartbeat_store

    current = heartbeat_store.current_fencing_record(v2.COMPANION_DIR, bound["terminal_id"])
    assert current is not None
    assert current["generation"] == bound["generation"]
    assert current["attempt_id"] == bound["binding"]["attempt_id"]
    assert current["current_token"]["id"] == bound["binding"]["fencing_token_id"]
    return reservation_id, bound


# --------------------------------------------------------------------
# 1. The projection, from production
# --------------------------------------------------------------------


def _release_session(bound) -> None:
    """Hand the session back, so nothing holds it any more.

    The reservation row still records what was bound; the attachment
    store no longer records a holder.  That divergence is the whole point
    -- the row describes the past, and a control arriving now must be
    refused on the store's answer rather than the row's.
    """
    owner = {
        "provider": "kimi_cli",
        "native_session_id": SESSION_ID,
        "terminal_id": bound["terminal_id"],
        "generation": bound["generation"],
        "execution_mode": em.NATIVE_TUI,
    }
    native_attachment.mark_draining(**owner)
    native_attachment.release(
        **owner,
        proof=native_attachment.no_survivor_proof(
            **owner,
            pane_id=PANE_ID,
            process_identity=native_attachment.process_identity(
                pid=PANE_PID, start_marker=START_MARKER
            ),
            survivors=[],
            observed_at="2026-07-25T00:00:00Z",
            observer="test",
        ),
    )


class TestTheRealProjectionPublishesTheProvenIdentity:
    """The reported defect, asked of the function the endpoint calls."""

    @pytest.mark.asyncio
    async def test_a_bound_native_generation_projects_its_real_mode(
        self, isolated_memory_db, worktree, tmp_path, native
    ):
        _, bound = await _bound_native(worktree, tmp_path)

        identity = managed_launch.managed_control_identity(bound["terminal_id"])

        assert identity is not None
        assert identity["execution_mode"] == em.NATIVE_TUI
        assert identity["vintage"] == "v2"

    @pytest.mark.asyncio
    async def test_it_publishes_the_native_session_from_the_durable_binding(
        self, isolated_memory_db, worktree, tmp_path, native
    ):
        _, bound = await _bound_native(worktree, tmp_path)

        identity = managed_launch.managed_control_identity(bound["terminal_id"])

        assert identity["native_session_id"] == SESSION_ID
        assert identity["native_session_id"] == bound["binding"]["native_session_id"]

    @pytest.mark.asyncio
    async def test_the_process_identity_carries_its_start_marker(
        self, isolated_memory_db, worktree, tmp_path, native
    ):
        """A bare pid would be the forgeable half published alone.

        Pids recycle in both directions, so a stale one can match an
        unrelated live process and forge a survivor, or match nothing and
        forge a no-survivor.  The marker is what makes the scalar mean one
        exact process, and it is asserted as a literal here because the
        rendering is a wire contract a consumer compares against.
        """
        _, bound = await _bound_native(worktree, tmp_path)

        identity = managed_launch.managed_control_identity(bound["terminal_id"])

        assert identity["provider_process_id"] == PROVEN_PROCESS_ID
        assert identity["provider_process_id"] != PANE_PID
        assert identity["provider_process_id"] != str(PANE_PID)

    @pytest.mark.asyncio
    async def test_the_bound_provider_version_travels_with_it(
        self, isolated_memory_db, worktree, tmp_path, native
    ):
        """The composer pin is a fact about the build that is running."""
        _, bound = await _bound_native(worktree, tmp_path)

        identity = managed_launch.managed_control_identity(bound["terminal_id"])

        assert identity["provider_version"] == PINNED_VERSION_BANNER

    @pytest.mark.asyncio
    async def test_kimi_is_projected_despite_its_no_proof_readiness_sibling(
        self, isolated_memory_db, worktree, tmp_path, native
    ):
        """§24.10.1's no-proof sibling must not brick every Kimi control.

        Kimi's published sibling deliberately carries no provider-authored
        identity keys -- its readiness is an observed attached pane, not a
        claim the provider makes about itself.  A projection that read the
        sibling would find nothing here and refuse every healthy Kimi
        generation, which is a rule about *proof class* turned against the
        provider it was never about.

        So this asserts both halves at once: the sibling really is
        no-proof, and the projection really did produce the identity
        anyway -- which is only possible if it read the binding and the
        attachment instead.
        """
        _, bound = await _bound_native(worktree, tmp_path)
        sibling = bound.get("native_readiness") or {}

        identity = managed_launch.managed_control_identity(bound["terminal_id"])

        assert sibling.get("provider_session_id") is None
        assert sibling.get("provider_process_id") is None
        assert identity["native_session_id"] == SESSION_ID
        assert identity["provider_process_id"] == PROVEN_PROCESS_ID

    @pytest.mark.asyncio
    async def test_the_resolved_control_identity_carries_it_to_the_wire(
        self, isolated_memory_db, worktree, tmp_path, native, monkeypatch
    ):
        """The endpoint's own view, not just the managed record beneath it."""
        _, bound = await _bound_native(worktree, tmp_path)
        monkeypatch.setattr(cis, "_tmux_client", lambda: None)

        resolved = cis.resolve_control_identity(bound["terminal_id"])

        assert resolved.execution_mode == cis.EXECUTION_MODE_NATIVE_TUI
        assert resolved.native_session_id == SESSION_ID
        assert resolved.provider_process_id == PROVEN_PROCESS_ID
        view = resolved.expected_identity_view()
        assert view["execution_mode"] == cis.EXECUTION_MODE_NATIVE_TUI
        assert view["provider_process_id"] == PROVEN_PROCESS_ID

    @pytest.mark.asyncio
    async def test_the_gate_lets_the_real_projection_through(
        self, isolated_memory_db, worktree, tmp_path, native, monkeypatch
    ):
        """The whole point: this generation was previously unreachable."""
        _, bound = await _bound_native(worktree, tmp_path)
        monkeypatch.setattr(cis, "_tmux_client", lambda: None)

        resolved = cis.resolve_control_identity(bound["terminal_id"])

        assert cis.screen_expected_identity({}, resolved) is None
        assert cis._native_identity_refusal(resolved) is None


class TestTheRefusalsThatMustSurvive:
    """A projection that always succeeds is as broken as one that never did."""

    @pytest.mark.asyncio
    async def test_a_released_session_is_refused_not_projected(
        self, isolated_memory_db, worktree, tmp_path, native, monkeypatch
    ):
        """The attachment store is the authority, not the reservation row.

        The row still records what was bound.  Once the session is no
        longer held, the identity is gone -- and a control that arrives
        afterwards must not be delivered on the strength of a row that
        describes the past.
        """
        _, bound = await _bound_native(worktree, tmp_path)
        _release_session(bound)
        monkeypatch.setattr(cis, "_tmux_client", lambda: None)

        identity = managed_launch.managed_control_identity(bound["terminal_id"])
        resolved = cis.resolve_control_identity(bound["terminal_id"])

        assert identity["native_session_id"] is None
        assert identity["provider_process_id"] is None
        assert identity["native_identity_refusal"] is not None
        refusal = cis._native_identity_refusal(resolved)
        assert refusal is not None
        assert refusal[0] in (cis.REASON_IDENTITY_MISMATCH, cis.REASON_LINEAGE_UNPROVEN)

    @pytest.mark.asyncio
    async def test_an_unidentifiable_generation_refuses_before_the_durable_intent(
        self, isolated_memory_db, worktree, tmp_path, native, wired
    ):
        """Refused with the identity reason, and before any record exists.

        Position is the assertion, not just the outcome.  Every other
        identity refusal on this path is decided before ``open_intent``,
        so this one is too -- a control that can never be delivered must
        not leave a durable intent behind, and the caller must be told
        *why* rather than being handed whatever the write path failed on
        next.

        Without the request-level check the delivery still refuses, one
        step later, from inside the lease -- so an outcome-only assertion
        passes either way and proves nothing about the ordering.
        """
        tmux, journal = wired
        _, bound = await _bound_native(worktree, tmp_path)
        _release_session(bound)

        result = _deliver(bound["terminal_id"], tmux, journal, control_id="c-unidentified")

        assert result.outcome == cis.REFUSED
        assert result.reason_code in (cis.REASON_IDENTITY_MISMATCH, cis.REASON_LINEAGE_UNPROVEN)
        assert tmux.events == []
        assert journal.find("c-unidentified") is None

    @pytest.mark.asyncio
    async def test_an_unreadable_reservation_is_unproven_not_a_bridge_pane(
        self, isolated_memory_db, worktree, tmp_path, native, wired, monkeypatch
    ):
        """A row that could not be read has no mode, and must not borrow one."""
        tmux, journal = wired
        _, bound = await _bound_native(worktree, tmp_path)

        def _unreadable(_reservation_id):
            raise managed_launch.ManagedLaunchUnavailable("corrupt binding_json")

        monkeypatch.setattr(v2, "get", _unreadable)

        resolved = cis.resolve_control_identity(bound["terminal_id"])
        result = _deliver(bound["terminal_id"], tmux, journal, control_id="c-unreadable")

        assert resolved.execution_mode == cis.EXECUTION_MODE_ACP
        assert result.outcome == cis.REFUSED
        assert result.reason_code == cis.REASON_LINEAGE_UNPROVEN
        assert result.reason_code != cis.REASON_MANAGED_ACP_PANE
        assert tmux.events == []

    @pytest.mark.asyncio
    async def test_an_acp_generation_still_projects_acp(
        self, isolated_memory_db, worktree, tmp_path, native, monkeypatch
    ):
        """The half that must not move, driven through the real store."""
        record, _ = v2.reserve(_reserve_request(worktree, tmp_path, execution_mode="acp"))
        v2.claim_launch(record["reservation_id"])

        identity = managed_launch.managed_control_identity(record["terminal_id"])

        assert identity["execution_mode"] == em.ACP
        assert identity["native_session_id"] is None
        assert identity["provider_process_id"] is None
        # An ACP row is behaving correctly, so it is not reported as a
        # native identity that failed to resolve.
        assert identity["native_identity_refusal"] is None


class TestTheLiveReverificationBeforeAnyWrite:
    """The projection describes the past; the writer re-asks the question.

    A control arrives arbitrarily later than the bind that authorised it.
    In between, the pane can die and be replaced, or a successor
    generation can take the session over -- and every earlier check would
    still have passed honestly.
    """

    @pytest.mark.asyncio
    async def test_a_replaced_process_is_refused_live(
        self, isolated_memory_db, worktree, tmp_path, native
    ):
        reservation_id, _ = await _bound_native(worktree, tmp_path)
        # Same pane, different process: exactly what a provider that died
        # and was restarted in place looks like from outside.
        native.observed_pid = PANE_PID + 1

        with pytest.raises(managed_launch.ManagedLaunchConflict):
            managed_launch.verify_managed_native_identity(reservation_id)

    @pytest.mark.asyncio
    async def test_a_recycled_pid_with_a_new_start_marker_is_refused(
        self, isolated_memory_db, worktree, tmp_path, native
    ):
        """The exact case a bare pid could not catch."""
        reservation_id, _ = await _bound_native(worktree, tmp_path)
        native.observed_start_marker = "Thu Jul 24 11:30:00 2026"

        with pytest.raises(managed_launch.ManagedLaunchConflict):
            managed_launch.verify_managed_native_identity(reservation_id)

    @pytest.mark.asyncio
    async def test_a_vanished_pane_is_a_conflict(
        self, isolated_memory_db, worktree, tmp_path, native
    ):
        reservation_id, _ = await _bound_native(worktree, tmp_path)
        native.pane_gone = True

        with pytest.raises(managed_launch.ManagedLaunchConflict):
            managed_launch.verify_managed_native_identity(reservation_id)

    @pytest.mark.asyncio
    async def test_an_unreadable_pane_is_unavailable_not_a_conflict(
        self, isolated_memory_db, worktree, tmp_path, native
    ):
        """ "The pane is gone" and "we could not look" are different facts.

        They license opposite handling, and reporting the second as the
        first would close a delivery that is still open.
        """
        reservation_id, _ = await _bound_native(worktree, tmp_path)
        native.pane_unreadable = True

        with pytest.raises(managed_launch.ManagedLaunchUnavailable):
            managed_launch.verify_managed_native_identity(reservation_id)

    @pytest.mark.asyncio
    async def test_a_healthy_generation_verifies(
        self, isolated_memory_db, worktree, tmp_path, native
    ):
        """The falsifiability guard for the four refusals above."""
        reservation_id, bound = await _bound_native(worktree, tmp_path)

        proven = managed_launch.verify_managed_native_identity(reservation_id)

        assert proven["native_session_id"] == SESSION_ID
        assert proven["pane_id"] == PANE_ID
        assert v2.published_process_id(proven["process_identity"]) == PROVEN_PROCESS_ID

    @pytest.mark.asyncio
    async def test_the_control_deadline_reaches_the_live_native_observer(
        self, isolated_memory_db, worktree, tmp_path, native
    ):
        reservation_id, _ = await _bound_native(worktree, tmp_path)
        deadline = time.monotonic() + 1.0

        managed_launch.verify_managed_native_identity(
            reservation_id,
            deadline_monotonic=deadline,
        )

        assert native.observed_deadlines[-1] == deadline


# --------------------------------------------------------------------
# 2. Delivery through the provider's own adapter
# --------------------------------------------------------------------


class _FakeTmux:
    """The identity-bound write primitives, recorded rather than run.

    Records literal writes and named keys as distinct events, because the
    distinction is the whole contract under test: a raw literal line and
    a planned composer write both "succeed", and only the event sequence
    tells them apart.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.server_identities: list[Optional[str]] = []
        self.fail_key = False

    def pane_control_identity(self, *, pane_id: str, deadline_monotonic=None) -> Any:
        from cli_agent_orchestrator.clients.tmux import PaneControlIdentity

        return PaneControlIdentity(
            pane_id=pane_id,
            window_id=WINDOW_ID,
            session_id="$1",
            pane_pid=PANE_PID,
            session_name="cao-test",
            window_name="w-1",
            bracketed_paste_proven=False,
            dead=False,
            server_socket_path=SERVER_SOCKET,
        )

    def send_literal_line(
        self,
        pane_id,
        text,
        submit=True,
        *,
        expected_server_identity,
        deadline_monotonic=None,
    ):
        self.server_identities.append(expected_server_identity)
        chunks = 0
        if text:
            self.events.append(("literal", text))
            chunks = 1
        if submit:
            self.events.append(("enter", ""))
        return chunks

    def send_control_key(self, pane_id, key, *, expected_server_identity, deadline_monotonic=None):
        from cli_agent_orchestrator.clients.tmux import COMPOSER_CONTROL_KEYS

        assert key in COMPOSER_CONTROL_KEYS
        self.server_identities.append(expected_server_identity)
        if self.fail_key:
            raise RuntimeError("staged keystroke failure")
        self.events.append(("key", key))

    # The cond-0178 copy-mode guard primitives.  Default: the pane is
    # provably not in copy mode, so no exit control is ever recorded for a
    # test that does not model the wheel path.
    def pane_in_copy_mode(self, pane_id, *, expected_server_identity, deadline_monotonic=None):
        return False

    def send_copy_mode_cancel(self, pane_id, *, expected_server_identity, deadline_monotonic=None):
        self.server_identities.append(expected_server_identity)
        self.events.append(("copy-mode-cancel", ""))
        return True


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """The control path with tmux faked and everything else production."""
    tmux = _FakeTmux()
    monkeypatch.setattr(cis, "_tmux_client", lambda: tmux)
    journal = ControlInputJournal(tmp_path / "control-input.sqlite3")
    monkeypatch.setattr(cis, "get_control_input_journal", lambda: journal)
    return tmux, journal


def _deliver(terminal_id, tmux, journal, *, control_id="c-1", text="/compact", enter=True, **kw):
    return cis.deliver_control_input(
        terminal_id,
        control_id=control_id,
        text=text,
        enter=enter,
        journal=journal,
        **kw,
    )


class TestTheSendGoesThroughTheProviderAdapter:
    """Asserted on the adapter being *invoked*, never on success alone.

    ``send_literal_line`` would also "succeed" while typing raw bytes at
    an Ink composer -- no proven newline keystroke, no paste-burst reset,
    no submit settle -- and the Enter would be swallowed with no error
    and no turn.  So a test that only checked the outcome would pass
    against the exact defect this closes.
    """

    @pytest.mark.asyncio
    async def test_the_keystroke_sequence_is_the_adapters_plan(
        self, isolated_memory_db, worktree, tmp_path, native, wired
    ):
        tmux, journal = wired
        _, bound = await _bound_native(worktree, tmp_path)

        result = _deliver(bound["terminal_id"], tmux, journal)

        assert result.outcome == cis.ACCEPTED, (result.reason_code, result.detail)
        # The pinned burst reset between the text and the Enter is the
        # signature of the adapter's plan. A raw literal write produces
        # only ("literal", ...) then ("enter", "").
        assert tmux.events == [("literal", "/compact"), ("key", "End"), ("enter", "")]

    @pytest.mark.asyncio
    async def test_the_adapter_is_actually_called(
        self, isolated_memory_db, worktree, tmp_path, native, wired, monkeypatch
    ):
        """Named directly, so a future rewrite cannot quietly drop it."""
        tmux, journal = wired
        _, bound = await _bound_native(worktree, tmp_path)
        calls: list[Any] = []
        real = knc.execute_composer_plan

        def _spy(**kwargs):
            calls.append(kwargs)
            return real(**kwargs)

        monkeypatch.setattr(knc, "execute_composer_plan", _spy)

        result = _deliver(bound["terminal_id"], tmux, journal)

        assert result.outcome == cis.ACCEPTED, (result.reason_code, result.detail)
        assert len(calls) == 1
        assert calls[0]["plan"]["provider_version"] == PINNED_VERSION_BANNER

    @pytest.mark.asyncio
    async def test_every_keystroke_is_bound_to_the_proven_tmux_server(
        self, isolated_memory_db, worktree, tmp_path, native, wired
    ):
        """§24.7 must not weaken on the way through the adapter.

        A composer keystroke aimed at ``%7`` on the wrong tmux server
        lands in a stranger's composer exactly as a literal write would,
        so the named keys carry the same binding as the text.
        """
        tmux, journal = wired
        _, bound = await _bound_native(worktree, tmp_path)

        _deliver(bound["terminal_id"], tmux, journal)

        assert tmux.server_identities
        assert set(tmux.server_identities) == {SERVER_SOCKET}

    @pytest.mark.asyncio
    async def test_without_enter_nothing_is_submitted(
        self, isolated_memory_db, worktree, tmp_path, native, wired
    ):
        """Submission is the irreversible half and is stated, never inferred."""
        tmux, journal = wired
        _, bound = await _bound_native(worktree, tmp_path)

        result = _deliver(bound["terminal_id"], tmux, journal, enter=False)

        assert result.outcome == cis.ACCEPTED, (result.reason_code, result.detail)
        assert ("enter", "") not in tmux.events
        assert tmux.events == [("literal", "/compact")]

    @pytest.mark.asyncio
    async def test_a_replay_of_the_same_control_id_types_nothing_twice(
        self, isolated_memory_db, worktree, tmp_path, native, wired
    ):
        """At-most-once, answered from the durable record."""
        tmux, journal = wired
        _, bound = await _bound_native(worktree, tmp_path)

        first = _deliver(bound["terminal_id"], tmux, journal)
        typed_once = list(tmux.events)
        second = _deliver(bound["terminal_id"], tmux, journal)

        assert first.outcome == cis.ACCEPTED
        assert second.outcome == cis.ACCEPTED
        assert tmux.events == typed_once


class TestTheGatesRunBeforeTheWriteClaim:
    """A zero-byte outcome must be recorded as the refusal it truthfully is.

    The journal has no ``(writing, refused)`` edge, so anything decided
    after the claim can only be encoded as ``ambiguous`` -- which
    withholds the re-attempt a proven zero-byte refusal is entitled to
    grant.  Every gate that can fail therefore runs before the claim.
    """

    @pytest.mark.asyncio
    async def test_a_native_identity_timeout_is_bounded_and_releases_the_lease(
        self, isolated_memory_db, worktree, tmp_path, native, wired, monkeypatch
    ):
        tmux, journal = wired
        _, bound = await _bound_native(worktree, tmp_path)
        native.pane_timeout = True
        monkeypatch.setattr(cis, "WRITE_DEADLINE_SECONDS", 0.05)

        started = time.monotonic()
        result = _deliver(
            bound["terminal_id"],
            tmux,
            journal,
            control_id="c-native-timeout",
        )
        elapsed = time.monotonic() - started

        assert result.outcome == cis.REFUSED
        assert result.reason_code == cis.REASON_WRITE_DEADLINE
        assert result.as_response()["reattemptable"] is True
        assert elapsed < 0.15
        assert tmux.events == []
        assert journal.get("c-native-timeout").state == STATE_REFUSED

        native.pane_timeout = False
        monkeypatch.setattr(cis, "WRITE_DEADLINE_SECONDS", 5.0)
        healthy = _deliver(
            bound["terminal_id"],
            tmux,
            journal,
            control_id="c-after-native-timeout",
        )
        assert healthy.outcome == cis.ACCEPTED

    @pytest.mark.asyncio
    async def test_an_unproven_build_refuses_with_nothing_typed(
        self, isolated_memory_db, worktree, tmp_path, native, wired, monkeypatch
    ):
        tmux, journal = wired
        _, bound = await _bound_native(worktree, tmp_path)
        # A build that launch supports but whose composer was never read.
        # The two tables are deliberately separate -- the launch pin says
        # "this build is supported", the composer pin says "these exact
        # keystrokes were read out of this build" -- so they can and do
        # disagree, and this is that disagreement rather than a version
        # bind would have refused outright.
        monkeypatch.delitem(knc._PROVEN_COMPOSER_NEWLINE, "0.29.0")

        result = _deliver(bound["terminal_id"], tmux, journal)

        assert result.outcome == cis.REFUSED
        assert result.reason_code == cis.REASON_PROVIDER_UNSUPPORTED
        assert tmux.events == []
        assert result.outcome != cis.AMBIGUOUS

    @pytest.mark.asyncio
    async def test_the_refusal_is_durable_so_a_lost_response_is_answerable(
        self, isolated_memory_db, worktree, tmp_path, native, wired, monkeypatch
    ):
        tmux, journal = wired
        _, bound = await _bound_native(worktree, tmp_path)
        # A build that launch supports but whose composer was never read.
        # The two tables are deliberately separate -- the launch pin says
        # "this build is supported", the composer pin says "these exact
        # keystrokes were read out of this build" -- so they can and do
        # disagree, and this is that disagreement rather than a version
        # bind would have refused outright.
        monkeypatch.delitem(knc._PROVEN_COMPOSER_NEWLINE, "0.29.0")

        _deliver(bound["terminal_id"], tmux, journal, control_id="c-lost")
        recorded = journal.find("c-lost")

        assert recorded is not None
        assert recorded.reason_code == cis.REASON_PROVIDER_UNSUPPORTED

    @pytest.mark.asyncio
    async def test_a_process_replaced_before_the_write_refuses(
        self, isolated_memory_db, worktree, tmp_path, native, wired
    ):
        """Re-proven under the lease, not taken from the earlier projection."""
        tmux, journal = wired
        _, bound = await _bound_native(worktree, tmp_path)
        native.observed_pid = PANE_PID + 7

        result = _deliver(bound["terminal_id"], tmux, journal)

        assert result.outcome == cis.REFUSED
        assert result.reason_code == cis.REASON_IDENTITY_MISMATCH
        assert tmux.events == []

    @pytest.mark.asyncio
    async def test_an_unobservable_pane_refuses_as_unproven_not_as_gone(
        self, isolated_memory_db, worktree, tmp_path, native, wired
    ):
        """The conflict/unavailable split is not collapsed on the way out."""
        tmux, journal = wired
        _, bound = await _bound_native(worktree, tmp_path)
        native.pane_unreadable = True

        result = _deliver(bound["terminal_id"], tmux, journal)

        assert result.outcome == cis.REFUSED
        assert result.reason_code == cis.REASON_LINEAGE_UNPROVEN
        assert result.reason_code != cis.REASON_IDENTITY_MISMATCH
        assert tmux.events == []

    @pytest.mark.asyncio
    async def test_a_failure_after_the_claim_is_ambiguous(
        self, isolated_memory_db, worktree, tmp_path, native, wired
    ):
        """The other side of the boundary, and it must stay pessimistic.

        The burst-reset keystroke is sent after the payload and after the
        claim.  What reached the composer is bounded but not knowable, so
        the control must not be sent again.
        """
        tmux, journal = wired
        _, bound = await _bound_native(worktree, tmp_path)
        tmux.fail_key = True

        result = _deliver(bound["terminal_id"], tmux, journal)

        assert result.outcome == cis.AMBIGUOUS
        assert result.chunks_sent == 1
        assert result.enter_attempted is False


class TestTheSentinelAndStaleRefusalsAreUnchanged:
    """Nothing the native route added may weaken the existing screens."""

    @pytest.mark.asyncio
    async def test_bracketed_paste_framing_refuses_before_any_byte(
        self, isolated_memory_db, worktree, tmp_path, native, wired
    ):
        tmux, journal = wired
        _, bound = await _bound_native(worktree, tmp_path)

        result = _deliver(bound["terminal_id"], tmux, journal, text="/compact\x1b[200~")

        assert result.outcome == cis.REFUSED
        assert result.reason_code == cis.REASON_ILLEGAL_CONTROL_BYTES
        assert tmux.events == []

    @pytest.mark.asyncio
    async def test_a_stale_generation_refuses_before_any_byte(
        self, isolated_memory_db, worktree, tmp_path, native, wired
    ):
        tmux, journal = wired
        _, bound = await _bound_native(worktree, tmp_path)

        result = _deliver(
            bound["terminal_id"],
            tmux,
            journal,
            expected_identity={"terminal_generation": str(uuid.uuid4())},
        )

        assert result.outcome == cis.REFUSED
        assert result.reason_code == cis.REASON_STALE_GENERATION
        assert tmux.events == []

    @pytest.mark.asyncio
    async def test_a_bare_pid_expectation_does_not_match_the_proven_identity(
        self, isolated_memory_db, worktree, tmp_path, native, wired
    ):
        """The forgeable half alone must not satisfy the binding."""
        tmux, journal = wired
        _, bound = await _bound_native(worktree, tmp_path)

        result = _deliver(
            bound["terminal_id"],
            tmux,
            journal,
            expected_identity={"provider_process_id": str(PANE_PID)},
        )

        assert result.outcome == cis.REFUSED
        assert result.reason_code == cis.REASON_IDENTITY_MISMATCH
        assert tmux.events == []

    @pytest.mark.asyncio
    async def test_the_full_process_identity_expectation_is_honoured(
        self, isolated_memory_db, worktree, tmp_path, native, wired
    ):
        """Falsifiability guard: the check above must not refuse everything."""
        tmux, journal = wired
        _, bound = await _bound_native(worktree, tmp_path)

        result = _deliver(
            bound["terminal_id"],
            tmux,
            journal,
            expected_identity={
                "provider_process_id": PROVEN_PROCESS_ID,
                "native_session_id": SESSION_ID,
                "execution_mode": em.NATIVE_TUI,
            },
        )

        assert result.outcome == cis.ACCEPTED, (result.reason_code, result.detail)
        assert tmux.events == [("literal", "/compact"), ("key", "End"), ("enter", "")]
