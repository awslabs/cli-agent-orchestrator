"""Admission on a native generation, which owns no ACP bridge.

A native launch deliberately starts no bridge process: the pane's own
primary process is the provider's TUI.  So the bridge socket an ACP
admission talks to is not merely unnecessary here, it never exists —
and an admission that waited on it would block until timeout and then
record an ambiguity about bytes that were never sent.

These tests prove the branch that replaces it.  The shape they check is
the one that makes the two claims separable: *admission* means this
exact task was typed into this exact bound session exactly once, while
*provider acceptance* means the TUI took the turn.  The first is what
the transport can prove; the second is not, and stays open until
something observes the pane.  A suite that let the first imply the
second would be certifying started work that may still be sitting in a
composer.
"""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
import threading
import uuid
from typing import Any, Mapping, Optional

import pytest

from cli_agent_orchestrator.clients import database as _db
from cli_agent_orchestrator.models.managed_launch_v2 import (
    PROTOCOL_VERSION_V2,
    ManagedLaunchV2AdmitRequest,
    ManagedLaunchV2BindRequest,
    ManagedLaunchV2ReserveRequest,
)
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import execution_mode as em
from cli_agent_orchestrator.services import generation_fence as gf
from cli_agent_orchestrator.services import kimi_native_bootstrap as boot
from cli_agent_orchestrator.services import kimi_native_control as knc
from cli_agent_orchestrator.services import managed_launch_v2 as v2
from cli_agent_orchestrator.services import managed_provider_bridge as bridge
from cli_agent_orchestrator.services import native_pane_input as npi
from cli_agent_orchestrator.services import native_tui_launch
from cli_agent_orchestrator.services import stable_agent_roster as roster
from cli_agent_orchestrator.services.canonical_json import canonical_sha256
from cli_agent_orchestrator.services.managed_launch import (
    ManagedLaunchConflict,
    ManagedLaunchUnavailable,
)

PINNED_VERSION_BANNER = "kimi 0.29.0"
SESSION_ID = "session_9f2c41ab"
DELIVERY_ID = "44444444-4444-4444-8444-444444444444"
MODEL = "gpt-5.6-sol"
EFFORT = "xhigh"
PANE_ID = "%7"
PANE_PID = 4321
START_MARKER = "Thu Jul 24 10:00:00 2026"
TASK_MESSAGE = "review the exact head"


@pytest.fixture(autouse=True)
def _companion(tmp_path, monkeypatch):
    monkeypatch.setattr(v2, "COMPANION_DIR", tmp_path / "companion")
    monkeypatch.setattr("cli_agent_orchestrator.constants.COMPANION_DIR", tmp_path / "companion")
    monkeypatch.setattr(bridge, "BRIDGE_ROOT", tmp_path / "bridge")


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
        "delivery_id": DELIVERY_ID,
        "launch_nonce": "n" * 40,
        "execution_mode": "native_tui",
    }
    payload.update(changes)
    return ManagedLaunchV2ReserveRequest(**payload)


def _options() -> list[dict[str, Any]]:
    return [
        {"id": "model", "category": "model", "currentValue": "kimi-default"},
        {"id": "thinking", "category": "thought_level", "currentValue": "low"},
    ]


class _FakeAcp:
    """The zero-prompt bootstrap transport: mints an id, sends no turn."""

    def __init__(self) -> None:
        self._options = _options()

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


class _Keystrokes:
    """Everything that was typed at the pane, in order.

    Records the literal write and the submitting key as separate events
    because that separation is the contract: a payload that carried its
    own newline would submit itself, and a recorder that concatenated
    them could not tell the difference.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.panes: list[str] = []
        self.fail_literal = False
        self.fail_enter = False
        self.fail_key = False

    def transport_for(self, pane_id: str, **_kwargs) -> Any:
        self.panes.append(pane_id)
        return self

    def send_literal(self, text: str) -> None:
        if self.fail_literal:
            raise npi.NativePaneInputUnavailable("staged literal failure")
        self.events.append(("literal", text))

    def send_enter(self) -> None:
        if self.fail_enter:
            raise npi.NativePaneInputUnavailable("staged enter failure")
        self.events.append(("enter", ""))

    def send_key(self, key: str) -> None:
        """A named composer keystroke, recorded like any other event.

        Absent from this fake until the version normalization landed, and
        its absence was invisible for the wrong reason: the banner
        ``kimi 0.29.0`` missed the bare-keyed pin, so the pinned burst
        reset was skipped and nothing ever called this. Once the lookup
        normalized, the pin resolved, the reset became reachable, and the
        missing method surfaced as an ambiguous admission -- a fake that
        was incomplete for a path the code could not previously take.
        """
        if self.fail_key:
            raise npi.NativePaneInputUnavailable("staged key failure")
        self.events.append(("key", key))

    @property
    def typed(self) -> list[str]:
        return [text for kind, text in self.events if kind == "literal"]


class _Pane:
    """The live pane observation the identity check compares against."""

    def __init__(self, state: "_Harness") -> None:
        self._state = state

    def observe(self) -> Optional[Mapping[str, Any]]:
        if self._state.pane_gone:
            return None
        return {
            "pane_id": self._state.observed_pane_id,
            "pid": self._state.observed_pid,
            "start_marker": self._state.observed_start_marker,
            "argv": ["kimi"],
        }


class _Harness:
    def __init__(self) -> None:
        self.terminals: list[dict[str, Any]] = []
        self.keystrokes = _Keystrokes()
        self.turn_state = TerminalStatus.IDLE
        # Set by a test that needs something to happen *during* the
        # readiness read -- the window a concurrent handler for the same
        # delivery id actually occupies.
        self.observe_turn_state: Optional[Any] = None
        self.pane_gone = False
        self.observed_pane_id = PANE_ID
        self.observed_pid = PANE_PID
        self.observed_start_marker = START_MARKER
        self.bridge_calls: list[dict[str, Any]] = []

    @property
    def launched_argv(self) -> list[str]:
        assert self.terminals, "no pane was ever created"
        return list(self.terminals[-1]["managed_native_command"])


@pytest.fixture
def harness(monkeypatch):
    state = _Harness()

    async def _create_terminal(**kwargs):
        state.terminals.append(kwargs)
        terminal_id = kwargs["reserved_terminal_id"]
        # A real row in the v2 store. The launch path writes one and later
        # steps read it back, so a fake that returned an id without a row
        # would test those steps against a terminal that does not exist.
        _db.create_terminal_v2(
            terminal_id,
            kwargs.get("session_name") or "cao-test",
            kwargs.get("window_name") or f"w-{terminal_id}",
            kwargs.get("provider") or "kimi_cli",
            generation=kwargs.get("terminal_generation"),
            pane_id="%7",
            window_id="@7",
            server_socket_path="/private/tmp/cao-native.sock",
            session_id="$1",
            pane_pid=4242,
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

    def _request_bridge(reservation_id, command, timeout=None):
        # Recorded, never reached on a native row. A native reservation
        # starts no bridge, so any call here is the defect this branch
        # exists to remove rather than a transport that merely fails.
        state.bridge_calls.append({"reservation_id": reservation_id, "command": command})
        return {
            "receipt": {
                "receipt_id": "turn-1",
                "provider_session_id": "thr_0192a7b4",
                "provider_turn_id": "turn-1",
                "provider_receipt_kind": "codex-turn-start",
            }
        }

    monkeypatch.setattr(boot, "StdioAcpBootstrap", lambda **kwargs: _FakeAcp())
    monkeypatch.setattr(bridge, "provider_version_banner", lambda *a, **k: PINNED_VERSION_BANNER)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.create_terminal", _create_terminal
    )
    monkeypatch.setattr(v2._V2NativePane, "observe", _launch_observe)
    monkeypatch.setattr(bridge, "request_bridge", _request_bridge)
    # The admission-time seams: the live pane read, the provider's own
    # idle reading of its own screen, and the keystroke transport.
    monkeypatch.setattr(native_tui_launch, "TmuxNativePane", lambda *a, **k: _Pane(state))

    def _turn_state(*args, **kwargs):
        if state.observe_turn_state is not None:
            return state.observe_turn_state(*args, **kwargs)
        return state.turn_state

    monkeypatch.setattr(npi, "observe_kimi_turn_state", _turn_state)
    monkeypatch.setattr(npi, "TmuxPaneInput", state.keystrokes.transport_for)
    return state


def _bind_request(record, attempt_id: Optional[str] = None):
    return ManagedLaunchV2BindRequest(
        protocol_version=PROTOCOL_VERSION_V2,
        terminal_id=record["terminal_id"],
        generation=record["generation"],
        attempt_id=attempt_id or str(uuid.uuid4()),
    )


def _admit_request(digest: str, **changes):
    payload = {
        "protocol_version": PROTOCOL_VERSION_V2,
        "delivery_id": DELIVERY_ID,
        "message": TASK_MESSAGE,
        "message_sha256": hashlib.sha256(TASK_MESSAGE.encode()).hexdigest(),
        "sender_id": "deadbeef",
        "orchestration_type": "assign",
        "context": {
            "boot_id": "11111111-1111-4111-8111-111111111111",
            "project": "test-project",
            "task_id": "test-task",
            "run_id": "test-task",
            "task_sha256": "1" * 64,
            "plan_sha256": "2" * 64,
            "dossier_sha256": "3" * 64,
            "lease_sha256": "4" * 64,
            "command_packet_sha256": "5" * 64,
            "source_chain_sha256": "6" * 64,
        },
        "native_binding_digest": digest,
    }
    payload.update(changes)
    return ManagedLaunchV2AdmitRequest(**payload)


async def _reserve_launch_bind(worktree, tmp_path):
    record, _ = v2.reserve(_reserve_request(worktree, tmp_path))
    assert record["execution_mode"] == em.NATIVE_TUI
    reservation_id = record["reservation_id"]
    launched = await v2.launch_reserved(reservation_id)
    bound = v2.bind_native(reservation_id, _bind_request(launched))
    assert bound["state"] == "bound"
    return reservation_id, bound


def _reserve_claim_bind_acp(worktree, tmp_path, monkeypatch):
    """An ACP row taken to ``bound`` without launching a native pane."""
    receipt = {
        "bridge_version": bridge.BRIDGE_VERSION,
        "receipt_id": "sess_acp_1",
        "provider_session_id": "sess_acp_1",
        "provider_receipt_kind": "kimi-acp-session-new",
        "provider_transcript_sha256": "a" * 64,
        "provider_version": PINNED_VERSION_BANNER,
        "model_input_ready": True,
        "reservation_id": None,
        "terminal_id": None,
        "generation": None,
        "provider": "kimi_cli",
        "agent_profile": "reviewer",
        "model": MODEL,
        "effort": EFFORT,
        "working_directory": str(worktree),
    }
    record, _ = v2.reserve(_reserve_request(worktree, tmp_path, execution_mode="acp"))
    assert record["execution_mode"] == em.ACP
    reservation_id = record["reservation_id"]
    v2.claim_launch(reservation_id)
    receipt.update(
        reservation_id=reservation_id,
        terminal_id=record["terminal_id"],
        generation=record["generation"],
    )
    monkeypatch.setattr(bridge, "read_state", lambda rid: {"state": "ready", "readiness": receipt})
    bound = v2.bind_native(reservation_id, _bind_request(record))
    assert bound["binding"]["execution_mode"] == em.ACP
    return reservation_id, bound


# --------------------------------------------------------------------
# 1. native bound → admitted, with zero ACP bridge artifacts
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_native_generation_reaches_admitted_without_any_bridge(
    isolated_memory_db, worktree, tmp_path, harness
):
    """The transition the fork could not previously make at all.

    Before this branch existed, a native row bound and then blocked on a
    socket no native launch ever creates, ending in a fabricated
    ambiguity about bytes that had not been sent.  The bridge assertion
    is therefore the point of the test, not a detail of it.
    """
    reservation_id, bound = await _reserve_launch_bind(worktree, tmp_path)
    digest = v2.native_binding_digest(bound)

    admitted = await v2.admit_reserved(reservation_id, _admit_request(digest))

    assert admitted["state"] == "admitted"
    assert harness.bridge_calls == []
    # i-0031 hardening: the roster incarnation reflects the admitted state.
    incarnation = roster.get_incarnation_by_terminal(
        admitted["terminal_id"], generation=admitted["generation"]
    )
    assert incarnation is not None
    assert incarnation["disposition"] == roster.INCARNATION_ADMITTED
    # Typed as one literal line into the exact bound pane, with the
    # submitting key as its own separate event.
    assert harness.keystrokes.panes == [PANE_ID]
    assert harness.keystrokes.events == [
        ("literal", TASK_MESSAGE),
        # The pinned burst reset, now reachable. It was absent from
        # this expectation only because the banner missed the
        # bare-keyed pin, so the pin resolved to nothing and the
        # reset was skipped -- the expectation recorded the defect.
        ("key", "End"),
        ("enter", ""),
    ]


@pytest.mark.asyncio
async def test_cancelled_native_admission_keeps_park_out_until_its_worker_finishes(
    isolated_memory_db, worktree, tmp_path, harness, monkeypatch
):
    """A cancelled awaiter cannot release byte ownership ahead of its worker."""
    reservation_id, bound = await _reserve_launch_bind(worktree, tmp_path)
    effect_started = threading.Event()
    release_effect = threading.Event()
    park_started = threading.Event()
    park_completed = threading.Event()
    original_literal = harness.keystrokes.send_literal
    real_create_task = asyncio.create_task
    effect_tasks = []

    def capture_effect_task(coro, *args, **kwargs):
        task = real_create_task(coro, *args, **kwargs)
        effect_tasks.append(task)
        return task

    monkeypatch.setattr(asyncio, "create_task", capture_effect_task)

    def blocking_literal(text):
        original_literal(text)  # the first provider-byte/effect proof
        effect_started.set()
        assert release_effect.wait(timeout=5)

    monkeypatch.setattr(harness.keystrokes, "send_literal", blocking_literal)
    admission = _admit_request(v2.native_binding_digest(bound))
    admission_task = real_create_task(v2.admit_reserved(reservation_id, admission))
    assert await asyncio.to_thread(effect_started.wait, 5)
    assert len(effect_tasks) == 1
    admission_task.cancel()
    record = v2.get(reservation_id)
    binding = record["binding"]
    park_request = {
        "schema": gf.PARK_REQUEST_SCHEMA,
        "operation_id": str(uuid.uuid4()),
        "reservation_id": reservation_id,
        "terminal_id": record["terminal_id"],
        "terminal_generation": record["generation"],
        "logical_task_id": record["task_id"],
        "retained_round": 0,
        "obligation_generation": record["obligation_generation"],
        "attempt_id": binding["attempt_id"],
        "report_sha256": "a" * 64,
    }

    def install_park():
        park_started.set()
        result = gf.install_park(
            v2.COMPANION_DIR,
            request=park_request,
            fencing_token_id=binding["fencing_token_id"],
        )
        park_completed.set()
        return result

    park_task = asyncio.create_task(asyncio.to_thread(install_park))
    assert await asyncio.to_thread(park_started.wait, 5)
    # Directly cancelling the effect-owning coroutine must still wait for
    # its executor Future; request cancellation alone is not the hard case.
    effect_tasks[0].cancel()
    # Teardown often sends a second cancellation while the first cleanup
    # await is in progress. It must not hand the lock back early.
    admission_task.cancel()
    # The worker is still between its first literal and remaining provider
    # effects. A completed receipt here would violate M3's absolute fence.
    assert not park_completed.is_set()
    assert not admission_task.done()

    release_effect.set()
    with pytest.raises(asyncio.CancelledError):
        await admission_task
    parked = await asyncio.wait_for(park_task, timeout=5)
    assert parked["outcome"] == gf.OUTCOME_FENCED
    after_receipt = list(harness.keystrokes.events)
    with pytest.raises(gf.FencedError):
        await v2.admit_reserved(reservation_id, admission)
    assert harness.keystrokes.events == after_receipt


@pytest.mark.asyncio
async def test_admission_records_posting_without_claiming_provider_acceptance(
    isolated_memory_db, worktree, tmp_path, harness
):
    """Delivery is proven; the turn is not, and the record says so.

    Writing to a pane cannot establish that the provider took the input
    -- the TUI may still be booting, or the text may sit in a composer.
    Keeping the two fields apart is what stops a caller reporting work
    as started on the strength of a successful keystroke.
    """
    reservation_id, bound = await _reserve_launch_bind(worktree, tmp_path)
    admitted = await v2.admit_reserved(
        reservation_id, _admit_request(v2.native_binding_digest(bound))
    )

    submission = admitted["admission"]["native_submission"]
    assert submission["schema"] == knc.RECORD_SCHEMA
    assert submission["kind"] == knc.KIND_QUEUE
    assert submission["posted"] is True
    assert submission["provider_accepted"] is False
    assert admitted["admission"]["provider_accepted"] is False
    # No ACP submission receipt was invented to stand in for the turn.
    assert "provider_submission_receipt" not in admitted["admission"]
    # The exact bytes are bound to the admitted message. The control
    # adapter digests the payload with its own canonical encoding, which is
    # deliberately not the admission's raw-bytes ``message_sha256`` -- so
    # the binding is checked in the adapter's convention rather than by
    # assuming the two agree, which they never do.
    assert submission["payload_sha256"] == canonical_sha256(TASK_MESSAGE)
    assert submission["payload_sha256"] != admitted["admission"]["message_sha256"]
    assert submission["native_session_id"] == SESSION_ID


# --------------------------------------------------------------------
# 2. binding-mismatch refusal, zero task admission and zero provider I/O
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_replaced_pane_process_refuses_with_zero_task_bytes(
    isolated_memory_db, worktree, tmp_path, harness
):
    """A recycled pid must not be able to inherit someone else's task.

    The pane id still matches and the pid is unchanged; only the process
    start marker differs, which is exactly the case a pid-only check
    would wave through.
    """
    reservation_id, bound = await _reserve_launch_bind(worktree, tmp_path)
    harness.observed_start_marker = "Thu Jul 24 11:30:00 2026"

    with pytest.raises(ManagedLaunchConflict, match="replaced"):
        await v2.admit_reserved(reservation_id, _admit_request(v2.native_binding_digest(bound)))

    assert harness.keystrokes.events == []
    # Zero task bytes, and the reason is durable rather than living only
    # in an HTTP response that a lost connection would destroy. A caller
    # that never received the answer re-reads the reservation and finds
    # the refusal, instead of a bare ``bound`` row it must treat as
    # maybe-delivered forever.
    after = v2.get(reservation_id)
    assert after["admission"]["status"] == "refused"
    assert after["admission"]["refusal_reason"] == v2.REFUSED_NATIVE_IDENTITY
    # A replaced process is not going to become the bound one by asking
    # again, so this refusal is closed rather than held open.
    assert after["admission"]["retryable"] is False
    assert after["state"] == "admitting"


@pytest.mark.asyncio
async def test_a_vanished_pane_refuses_rather_than_typing_into_its_replacement(
    isolated_memory_db, worktree, tmp_path, harness
):
    reservation_id, bound = await _reserve_launch_bind(worktree, tmp_path)
    harness.pane_gone = True

    with pytest.raises(ManagedLaunchConflict, match="no longer exists"):
        await v2.admit_reserved(reservation_id, _admit_request(v2.native_binding_digest(bound)))

    assert harness.keystrokes.events == []
    after = v2.get(reservation_id)
    assert after["admission"]["status"] == "refused"
    assert after["admission"]["retryable"] is False


@pytest.mark.asyncio
async def test_a_busy_pane_refuses_admission_with_zero_task_bytes(
    isolated_memory_db, worktree, tmp_path, harness
):
    """The idle gate, read from the provider's own view of its screen.

    ``PROCESSING`` also covers the boot window, which is the dangerous
    one: Kimi paints its status bar before it can accept input, and a
    task delivered there is absorbed with no error anywhere.

    The refusal is durable and *retryable*, which is the whole shape of
    this case: a pane that is booting will be ready shortly, so the
    delivery is held open rather than closed, and the row stays at the
    exact state a later claim requires.
    """
    reservation_id, bound = await _reserve_launch_bind(worktree, tmp_path)
    harness.turn_state = TerminalStatus.PROCESSING

    with pytest.raises(ManagedLaunchConflict, match="not idle"):
        await v2.admit_reserved(reservation_id, _admit_request(v2.native_binding_digest(bound)))

    assert harness.keystrokes.events == []
    after = v2.get(reservation_id)
    admission = after["admission"]
    assert admission["status"] == "refused"
    assert admission["refusal_reason"] == v2.REFUSED_PROVIDER_NOT_YET_READY
    assert admission["retryable"] is True
    assert admission["delivery_id"] == DELIVERY_ID
    # The evidence, not just the verdict: what was seen, of which pane.
    assert admission["readiness_observation"]["provider_status"] == (
        TerminalStatus.PROCESSING.value
    )
    assert admission["readiness_observation"]["pane_id"] == PANE_ID
    assert admission["readiness_observation"]["input_ready"] is False
    # Held at ``bound``: a refusal that advanced the row would become the
    # thing that blocks the retry it exists to invite.
    assert after["state"] == "bound"
    # Zero control-operation rows, so nothing was even opened, let alone
    # typed -- the delivery is provably absent from the pane's history.
    assert knc.get(DELIVERY_ID) is None


@pytest.mark.asyncio
async def test_the_same_delivery_is_admitted_exactly_once_after_the_pane_settles(
    isolated_memory_db, worktree, tmp_path, harness
):
    """The retry the refusal invites, and the duplicate it must not become.

    The same delivery id that was refused during boot is the one that
    completes -- no second reservation, no second binding, no second copy
    of the task.  The keystroke record is the proof that matters: one
    literal and one submitting key across both attempts.
    """
    reservation_id, bound = await _reserve_launch_bind(worktree, tmp_path)
    request = _admit_request(v2.native_binding_digest(bound))
    harness.turn_state = TerminalStatus.PROCESSING

    with pytest.raises(ManagedLaunchConflict, match="not idle"):
        await v2.admit_reserved(reservation_id, request)
    assert v2.get(reservation_id)["admission"]["status"] == "refused"

    harness.turn_state = TerminalStatus.IDLE
    admitted = await v2.admit_reserved(reservation_id, request)

    assert admitted["state"] == "admitted"
    assert admitted["admission"]["delivery_id"] == DELIVERY_ID
    # The refusal was superseded, not appended to: nothing of it survives
    # on a record that now says the task was delivered.
    assert "refusal_reason" not in admitted["admission"]
    assert harness.keystrokes.events == [
        ("literal", TASK_MESSAGE),
        # The pinned burst reset, now reachable. It was absent from
        # this expectation only because the banner missed the
        # bare-keyed pin, so the pin resolved to nothing and the
        # reset was skipped -- the expectation recorded the defect.
        ("key", "End"),
        ("enter", ""),
    ]


@pytest.mark.asyncio
async def test_repeated_pre_readiness_attempts_re_persist_one_refusal_and_nothing_else(
    isolated_memory_db, worktree, tmp_path, harness
):
    """Retrying while still booting must cost nothing anywhere.

    The invariant is not "the record is byte-identical" -- each attempt
    genuinely observed the pane again, and saying otherwise would be a
    fabricated observation.  It is that no *delivery-bearing* artifact
    accumulates: no control operation, no keystroke, no second admission,
    and no drift in what the refusal says.
    """
    reservation_id, bound = await _reserve_launch_bind(worktree, tmp_path)
    request = _admit_request(v2.native_binding_digest(bound))
    harness.turn_state = TerminalStatus.PROCESSING

    for _ in range(3):
        with pytest.raises(ManagedLaunchConflict, match="not idle"):
            await v2.admit_reserved(reservation_id, request)

    after = v2.get(reservation_id)
    assert after["state"] == "bound"
    assert after["admission"]["refusal_reason"] == v2.REFUSED_PROVIDER_NOT_YET_READY
    assert after["admission"]["retryable"] is True
    assert knc.get(DELIVERY_ID) is None
    assert harness.keystrokes.events == []
    assert harness.bridge_calls == []


@pytest.mark.asyncio
async def test_a_refusal_that_cannot_be_stored_is_never_reported_as_proven(
    isolated_memory_db, worktree, tmp_path, harness, monkeypatch
):
    """The failure that must not be papered over: an unstorable refusal.

    A refusal answered but not stored is worse than no refusal at all.
    The caller would be told "provably nothing was sent" while the only
    durable state stays a bare ``bound`` row with no admission -- and
    anyone who re-reads it, including the same caller after a lost
    response, sees the silence that has to be read as maybe-delivered.
    Two observers of one delivery would then disagree, which is exactly
    the false ambiguity this seam exists to remove.

    So the answer degrades to "unresolved" rather than up to "refused",
    which leaves the response and the stored state saying the same thing.
    """

    def _cannot_persist(*args, **kwargs):
        raise ManagedLaunchUnavailable("the reservation store is unreachable")

    reservation_id, bound = await _reserve_launch_bind(worktree, tmp_path)
    harness.turn_state = TerminalStatus.PROCESSING
    monkeypatch.setattr(v2, "refuse_admission_before_io", _cannot_persist)

    with pytest.raises(ManagedLaunchUnavailable, match="could not be recorded"):
        await v2.admit_reserved(reservation_id, _admit_request(v2.native_binding_digest(bound)))

    # No durable refusal was claimed, and none is pretended to exist.
    after = v2.get(reservation_id)
    assert after["state"] == "bound"
    assert after["admission"] is None
    # And it is still true that nothing was sent -- the honesty being
    # protected here is about what can be *proven* to a later reader.
    assert harness.keystrokes.events == []


# --------------------------------------------------------------------
# 3. admission response-loss reconciliation by exact id
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_lost_admission_response_reconciles_without_retyping(
    isolated_memory_db, worktree, tmp_path, harness
):
    """The replay answers from stored state and types nothing again.

    v2 has no reconcile endpoint; replaying the same delivery id is the
    recovery.  The delivery id is also the control operation id, so the
    exact operation is addressable — there is no scan and no "whatever
    we typed most recently".
    """
    reservation_id, bound = await _reserve_launch_bind(worktree, tmp_path)
    request = _admit_request(v2.native_binding_digest(bound))

    first = await v2.admit_reserved(reservation_id, request)
    replay = await v2.admit_reserved(reservation_id, request)

    assert first["state"] == replay["state"] == "admitted"
    assert replay["admission"]["native_submission"] == first["admission"]["native_submission"]
    # Exactly one delivery crossed the boundary, not two.
    assert harness.keystrokes.events == [
        ("literal", TASK_MESSAGE),
        # The pinned burst reset, now reachable. It was absent from
        # this expectation only because the banner missed the
        # bare-keyed pin, so the pin resolved to nothing and the
        # reset was skipped -- the expectation recorded the defect.
        ("key", "End"),
        ("enter", ""),
    ]
    assert harness.bridge_calls == []


@pytest.mark.asyncio
async def test_a_failed_submitting_key_is_ambiguous_and_never_retyped(
    isolated_memory_db, worktree, tmp_path, harness
):
    """Written but possibly unsubmitted is not the same as not sent.

    The payload reached the composer, so a retry would risk a second
    copy of the task.  The admission stays ambiguous and a replay
    reports that state rather than resending.
    """
    reservation_id, bound = await _reserve_launch_bind(worktree, tmp_path)
    harness.keystrokes.fail_enter = True
    request = _admit_request(v2.native_binding_digest(bound))

    result = await v2.admit_reserved(reservation_id, request)
    assert result["admission"]["status"] == "ambiguous_preserved"
    assert result["state"] == "admitting"

    typed_once = list(harness.keystrokes.events)
    replay = await v2.admit_reserved(reservation_id, request)
    assert replay["admission"]["status"] == "ambiguous_preserved"
    assert harness.keystrokes.events == typed_once


@pytest.mark.asyncio
async def test_a_transport_that_never_wrote_is_still_treated_as_ambiguous(
    isolated_memory_db, worktree, tmp_path, harness
):
    """Conservative on purpose: a raised transport is not proof of silence.

    The staged failure happens before the first chunk, so in fact nothing
    was typed — and the record still says ambiguous.  The adapter does not
    inspect the exception to decide, because the one case it would get
    wrong (a write that landed and then failed to report) is the case that
    produces a duplicated task.  A caller must reconcile by observation
    rather than infer non-delivery from an error.
    """
    reservation_id, bound = await _reserve_launch_bind(worktree, tmp_path)
    harness.keystrokes.fail_literal = True
    request = _admit_request(v2.native_binding_digest(bound))

    result = await v2.admit_reserved(reservation_id, request)

    assert result["admission"]["status"] == "ambiguous_preserved"
    assert result["state"] == "admitting"
    assert harness.keystrokes.typed == []


@pytest.mark.asyncio
async def test_a_crash_before_the_operation_opened_refuses_rather_than_guessing(
    isolated_memory_db, worktree, tmp_path, harness
):
    """The one delivery failure that *is* provably silent, and is named so.

    The adapter journals its intent before it touches a keyboard, so an
    admission that was claimed with no operation record behind it crashed
    between the two — provably before anything was typed.  That is a
    refusal, not an ambiguity, and the difference is what lets a caller
    act instead of treating the delivery as maybe-sent forever.
    """
    reservation_id, bound = await _reserve_launch_bind(worktree, tmp_path)
    request = _admit_request(v2.native_binding_digest(bound))

    # Exactly the surviving state of a crash after the claim commits: the
    # admission row exists, no control operation was ever opened.
    claimed, should_send = v2.claim_admission(reservation_id, request)
    assert should_send and claimed["admission"]["status"] == "io-attempted"
    assert knc.get(DELIVERY_ID) is None

    result = await v2.admit_reserved(reservation_id, request)

    assert result["admission"]["status"] == "refused"
    assert result["admission"]["refusal_reason"] == "no_control_operation"
    # The row is preserved rather than advanced, and nothing was typed to
    # find that out — the replay never reaches the pane at all.
    assert result["state"] == "admitting"
    assert harness.keystrokes.events == []
    assert harness.bridge_calls == []


# --------------------------------------------------------------------
# 4. the ACP branch is unchanged and still bridge-mediated
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acp_admission_still_goes_through_the_bridge(
    isolated_memory_db, worktree, tmp_path, harness, monkeypatch
):
    """The other half of the branch: ACP rows keep their exact path.

    Proven by where the task went rather than by reading the branch
    condition back — the bridge received the admit command and the
    native transport was never constructed.
    """
    reservation_id, bound = _reserve_claim_bind_acp(worktree, tmp_path, monkeypatch)

    def _acp_bridge(rid, command, timeout=None):
        harness.bridge_calls.append({"reservation_id": rid, "command": command})
        return {
            "receipt": {
                "receipt_id": "turn-1",
                "provider_session_id": "sess_acp_1",
                "provider_turn_id": "turn-1",
                "provider_receipt_kind": "kimi-session-update",
            }
        }

    monkeypatch.setattr(bridge, "request_bridge", _acp_bridge)

    admitted = await v2.admit_reserved(
        reservation_id, _admit_request(v2.native_binding_digest(bound))
    )

    assert admitted["state"] == "admitted"
    assert [call["command"]["op"] for call in harness.bridge_calls] == ["admit"]
    assert harness.bridge_calls[0]["command"]["message"] == TASK_MESSAGE
    # The ACP row completes on its provider-native submission receipt,
    # and the native transport was never touched.
    assert admitted["admission"]["provider_submission_receipt"]["provider_receipt_kind"] == (
        "kimi-session-update"
    )
    assert harness.keystrokes.events == []


@pytest.mark.asyncio
async def test_a_native_row_cannot_complete_on_an_acp_submission_receipt(
    isolated_memory_db, worktree, tmp_path, harness
):
    """The two completion paths are not interchangeable.

    An ACP receipt is not weaker evidence about a native run; it is
    evidence about a different thing, and accepting it here would let a
    native generation be marked admitted on a turn that happened
    somewhere else entirely.

    The receipt offered is deliberately *well formed* for this provider --
    the right kind, naming the bound session -- so the refusal can only
    come from the immutable mode.  A malformed one would prove nothing:
    it is rejected on the same path an ACP row would reject it.
    """
    reservation_id, bound = await _reserve_launch_bind(worktree, tmp_path)
    request = _admit_request(v2.native_binding_digest(bound))
    v2.claim_admission(reservation_id, request)

    with pytest.raises(ManagedLaunchConflict, match="never on a provider submission receipt"):
        v2.complete_admission(
            reservation_id,
            request.delivery_id,
            {
                "receipt_id": "turn-1",
                "provider_session_id": SESSION_ID,
                "provider_turn_id": "turn-1",
                "provider_receipt_kind": "kimi-session-update",
            },
        )

    # Refused, so the row still awaits its real completion.
    assert v2.get(reservation_id)["state"] == "admitting"


@pytest.mark.asyncio
async def test_an_acp_row_cannot_complete_on_a_native_control_record(
    isolated_memory_db, worktree, tmp_path, harness, monkeypatch
):
    """The converse guard, so neither path is a lenient version of the other.

    A keystroke record is a fact about a pane.  Crediting it to a row
    whose provider is being driven over a socket would certify a delivery
    into a session that never received one.
    """
    reservation_id, bound = _reserve_claim_bind_acp(worktree, tmp_path, monkeypatch)
    request = _admit_request(v2.native_binding_digest(bound))
    v2.claim_admission(reservation_id, request)

    operation = {
        "schema": knc.RECORD_SCHEMA,
        "kind": knc.KIND_QUEUE,
        "operation_id": request.delivery_id,
        "native_session_id": bound["binding"]["native_session_id"],
        "terminal_id": bound["terminal_id"],
        "generation": bound["generation"],
        "execution_mode": em.ACP,
        "posted": True,
        "payload_sha256": canonical_sha256(TASK_MESSAGE),
    }
    with pytest.raises(ManagedLaunchConflict, match="completes over its bridge path"):
        v2.complete_native_admission(
            reservation_id,
            request.delivery_id,
            operation,
            canonical_sha256(TASK_MESSAGE),
        )

    assert v2.get(reservation_id)["state"] == "admitting"


# --------------------------------------------------------------------
# 5. two handlers, one delivery id: no caller is told the wrong thing
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_refusal_that_loses_its_race_reports_the_winner_not_a_zero_byte_verdict(
    isolated_memory_db, worktree, tmp_path, harness
):
    """The lie a lost compare-and-swap could otherwise tell.

    Two handlers can hold the same delivery id at once -- a client that
    timed out and retried is the ordinary way it happens.  If one reads
    the pane as busy while the other has already claimed the admission
    and may be typing, the first must not answer "provably nothing was
    sent": that is a statement about the delivery, not about the request,
    and the delivery is in the other handler's hands.

    Staged deterministically by claiming the admission *during* the
    readiness read, which is exactly the window the real race occupies.
    """
    reservation_id, bound = await _reserve_launch_bind(worktree, tmp_path)
    request = _admit_request(v2.native_binding_digest(bound))

    def _busy_while_a_sibling_claims(*_args, **_kwargs):
        v2.claim_admission(reservation_id, request)
        return TerminalStatus.PROCESSING

    harness.observe_turn_state = _busy_while_a_sibling_claims

    result = await v2.admit_reserved(reservation_id, request)

    # No refusal was invented over the winner's record, and no exception
    # claimed one either.
    assert result["admission"]["status"] == "io-attempted"
    assert result["admission"]["delivery_id"] == DELIVERY_ID
    assert v2.get(reservation_id)["admission"]["status"] == "io-attempted"
    # This handler still sent nothing, which was always true; what changed
    # is that it no longer says so *about the delivery*.
    assert harness.keystrokes.events == []


@pytest.mark.asyncio
async def test_losing_the_claim_race_never_publishes_a_crash_verdict_on_a_live_sibling(
    isolated_memory_db, worktree, tmp_path, harness
):
    """The symmetric path: the claim CAS loses instead of the refusal CAS.

    An absent control operation is proof of a crash only when the handler
    that would have opened it is gone.  When it is alive and merely has
    not got there yet, the same absence means nothing -- and recording it
    as a refusal would mark a delivery provably-unsent moments before its
    bytes are written.
    """
    reservation_id, bound = await _reserve_launch_bind(worktree, tmp_path)
    request = _admit_request(v2.native_binding_digest(bound))

    def _idle_while_a_sibling_claims(*_args, **_kwargs):
        v2.claim_admission(reservation_id, request)
        return TerminalStatus.IDLE

    harness.observe_turn_state = _idle_while_a_sibling_claims

    result = await v2.admit_reserved(reservation_id, request)

    assert knc.get(DELIVERY_ID) is None
    assert result["admission"]["status"] == "io-attempted"
    assert "refusal_reason" not in result["admission"]
    assert harness.keystrokes.events == []


@pytest.mark.asyncio
async def test_a_replay_after_a_real_crash_still_refuses_on_the_absent_operation(
    isolated_memory_db, worktree, tmp_path, harness
):
    """The distinction above must not have disarmed the crash recovery.

    Same absent operation record, opposite conclusion, because the
    handler that would have opened it is gone rather than running: this
    request arrived fresh from the wire against a claim nobody holds.
    """
    reservation_id, bound = await _reserve_launch_bind(worktree, tmp_path)
    request = _admit_request(v2.native_binding_digest(bound))
    claimed, should_send = v2.claim_admission(reservation_id, request)
    assert should_send and claimed["admission"]["status"] == "io-attempted"

    result = await v2.admit_reserved(reservation_id, request)

    assert result["admission"]["status"] == "refused"
    assert result["admission"]["refusal_reason"] == "no_control_operation"


@pytest.mark.asyncio
async def test_a_lost_response_is_answered_from_the_reservation_in_both_directions(
    isolated_memory_db, worktree, tmp_path, harness
):
    """Response loss on either side of readiness, recovered by re-reading.

    This is the whole point of persisting the refusal.  Before readiness
    a lost response used to leave a bare ``bound`` row -- indistinguishable
    from a request still in flight, so the only safe reading was
    maybe-delivered, and a delivery that provably never happened stayed
    ambiguous forever.  After readiness the admitted receipt has to be
    equally re-readable, and neither direction may type anything a second
    time.
    """
    reservation_id, bound = await _reserve_launch_bind(worktree, tmp_path)
    request = _admit_request(v2.native_binding_digest(bound))

    # Direction one: the refusal's response is lost. A re-read finds the
    # refusal itself, with the evidence it rested on.
    harness.turn_state = TerminalStatus.PROCESSING
    with pytest.raises(ManagedLaunchConflict):
        await v2.admit_reserved(reservation_id, request)

    reread = v2.get(reservation_id)
    assert reread["admission"]["status"] == "refused"
    assert reread["admission"]["refusal_reason"] == v2.REFUSED_PROVIDER_NOT_YET_READY
    assert reread["admission"]["readiness_observation"]["input_ready"] is False
    assert reread["state"] == "bound"
    assert harness.keystrokes.events == []

    # Direction two: the admission succeeds and *that* response is lost.
    harness.turn_state = TerminalStatus.IDLE
    admitted = await v2.admit_reserved(reservation_id, request)
    assert admitted["state"] == "admitted"

    reread = v2.get(reservation_id)
    assert reread["admission"]["status"] == "admitted"
    assert reread["admission"]["native_submission"]["payload_sha256"] == canonical_sha256(
        TASK_MESSAGE
    )

    # And the replay that a lost response provokes types nothing again.
    replay = await v2.admit_reserved(reservation_id, request)
    assert replay["admission"]["native_submission"] == reread["admission"]["native_submission"]
    assert harness.keystrokes.events == [
        ("literal", TASK_MESSAGE),
        # The pinned burst reset, now reachable. It was absent from
        # this expectation only because the banner missed the
        # bare-keyed pin, so the pin resolved to nothing and the
        # reset was skipped -- the expectation recorded the defect.
        ("key", "End"),
        ("enter", ""),
    ]


@pytest.mark.asyncio
async def test_a_racing_delivery_id_carrying_other_bytes_is_refused_not_answered(
    isolated_memory_db, worktree, tmp_path, harness
):
    """Replay identity has to hold on the concurrent route too.

    A delivery id that comes back carrying a different message is a
    different delivery, and the ordinary replay check refuses it.  The
    race opens a second way in: if a sibling's record can be handed back
    *because* it won the write, the check is bypassed and one task's
    outcome is reported for another's bytes -- worse than a plain
    duplicate, because the answer looks authoritative.
    """
    reservation_id, bound = await _reserve_launch_bind(worktree, tmp_path)
    digest = v2.native_binding_digest(bound)
    mine = _admit_request(digest)

    def _busy_while_a_sibling_claims(*_args, **_kwargs):
        v2.claim_admission(reservation_id, mine)
        return TerminalStatus.PROCESSING

    harness.observe_turn_state = _busy_while_a_sibling_claims
    other_message = "a different task entirely"
    theirs = _admit_request(
        digest,
        message=other_message,
        message_sha256=hashlib.sha256(other_message.encode()).hexdigest(),
    )

    with pytest.raises(ManagedLaunchConflict, match="different admission identity"):
        await v2.admit_reserved(reservation_id, theirs)

    # The winner's record is untouched and still describes its own bytes.
    stored = v2.get(reservation_id)["admission"]
    assert stored["message_sha256"] == mine.message_sha256
    assert harness.keystrokes.events == []
