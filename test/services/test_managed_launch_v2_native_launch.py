"""The native-TUI branch of ``launch_reserved``, end to end over fakes.

The execution-mode suite proves a native reservation is *refused* by any
surface that has no native branch.  This suite proves the branch that now
exists actually runs the provider's own TUI: the pane's primary process
is the provider resuming the session the bootstrap minted, no ACP bridge
is started under it, and the readiness receipt it publishes is the
native-kind one that ``bind_native`` will accept.

The negative cases all assert the same thing from different directions —
that a native launch which cannot prove its preconditions leaves *no
pane behind*.  A pane started under an unproven bootstrap is the one
failure this design cannot recover from: two writers on a single-writer
provider session produce no error, only a transcript that later reads as
one confused run.
"""

from __future__ import annotations

import hashlib
import subprocess
import uuid
from typing import Any, Mapping, Optional

import pytest

from cli_agent_orchestrator.models.managed_launch_v2 import (
    PROTOCOL_VERSION_V2,
    ManagedLaunchV2ReserveRequest,
)
from cli_agent_orchestrator.services import execution_mode as em
from cli_agent_orchestrator.services import kimi_native_bootstrap as boot
from cli_agent_orchestrator.services import managed_launch_v2 as v2
from cli_agent_orchestrator.services import managed_provider_bridge as bridge
from cli_agent_orchestrator.services import native_tui_launch

PINNED_VERSION_BANNER = "kimi 0.29.0"
SESSION_ID = "session_9f2c41ab"
DELIVERY_ID = "44444444-4444-4444-8444-444444444444"
MODEL = "gpt-5.6-sol"
EFFORT = "xhigh"


@pytest.fixture(autouse=True)
def _companion(tmp_path, monkeypatch):
    monkeypatch.setattr(v2, "COMPANION_DIR", tmp_path / "companion")
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
        "agent_profile": "reviewer-sol-max",
        "caller_id": "deadbeef",
        "working_directory": str(worktree),
        # A codex-only field; a kimi reservation must leave it unset.
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
    """The provider's route options, as they read *before* the bootstrap."""
    return [
        {"id": "model", "category": "model", "currentValue": "kimi-default"},
        {"id": "thinking", "category": "thought_level", "currentValue": "low"},
    ]


class _FakeAcp:
    """A bootstrap transport that mints an id and honours the route.

    ``deaf_to_config`` models the provider that answers a config set
    successfully and changes nothing; ``exit_proof`` lets a test hand
    back an exit that was never actually proven.
    """

    def __init__(self, *, deaf_to_config: bool = False, exit_proof: Any = None) -> None:
        self.calls: list[str] = []
        self._options = _options()
        self._deaf = deaf_to_config
        self._exit_proof = exit_proof or {
            "pid": 4242,
            "exit_status": 0,
            "escalation": [boot.STEP_STDIN_CLOSED],
            "reaped": True,
        }

    def request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append(method)
        if method == "initialize":
            return {"protocolVersion": 1}
        if method == "session/new":
            return {"sessionId": SESSION_ID, "configOptions": self._options}
        if method == "session/set_config_option":
            if not self._deaf:
                for option in self._options:
                    if option["id"] == params["configId"]:
                        option["currentValue"] = params["value"]
            return {"configOptions": self._options}
        raise AssertionError(f"unexpected bootstrap method {method!r}")

    def terminate(self) -> Mapping[str, Any]:
        return self._exit_proof


class _Harness:
    """Every provider-facing boundary of a native launch, recorded.

    Nothing real is started: the point of the launch path is the order
    it does things in and the evidence it demands between the steps, and
    both are observable from what crossed each boundary.
    """

    def __init__(self) -> None:
        self.transport: Optional[_FakeAcp] = None
        self.bootstrap_kwargs: dict[str, Any] = {}
        self.terminals: list[dict[str, Any]] = []
        self.observed_pid = 4321

    @property
    def launched_argv(self) -> list[str]:
        assert self.terminals, "no pane was ever created"
        return list(self.terminals[-1]["managed_native_command"])


@pytest.fixture
def harness(monkeypatch):
    """Stage a native launch whose every real boundary is a fake.

    The bootstrap transport and the terminal are replaced; the bootstrap
    *logic*, the launch ordering, the attachment store, and the receipt
    are all real, because those are what this suite is about.
    """
    state = _Harness()

    def _transport(**kwargs):
        state.bootstrap_kwargs = kwargs
        assert state.transport is not None, "the test must stage a transport"
        return state.transport

    async def _create_terminal(**kwargs):
        state.terminals.append(kwargs)
        return {"terminal_id": kwargs["reserved_terminal_id"]}

    def _observe(self):
        return {
            "pane_id": "%7",
            "pid": state.observed_pid,
            "start_marker": "Thu Jul 24 10:00:00 2026",
            "argv": state.launched_argv,
        }

    monkeypatch.setattr(boot, "StdioAcpBootstrap", _transport)
    monkeypatch.setattr(bridge, "provider_version_banner", lambda *a, **k: PINNED_VERSION_BANNER)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.create_terminal", _create_terminal
    )
    monkeypatch.setattr(v2._V2NativePane, "observe", _observe)
    state.transport = _FakeAcp()
    return state


async def _launch(worktree, tmp_path, **changes):
    record, _ = v2.reserve(_reserve_request(worktree, tmp_path, **changes))
    assert record["execution_mode"] == em.NATIVE_TUI
    return record, await v2.launch_reserved(record["reservation_id"])


def _published_receipt(reservation_id: str) -> dict[str, Any]:
    state = bridge.read_state(reservation_id)
    assert state["state"] == "ready"
    return state["readiness"]


# --------------------------------------------------------------------
# The golden path
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_pane_runs_the_provider_resuming_the_minted_session(
    isolated_memory_db, worktree, tmp_path, harness
):
    """The pane's own primary process is the provider, not a bridge.

    This is the whole difference between the two modes.  In ACP the pane
    runs the bridge and the provider is its child; here the provider is
    the pane, which is what makes the TUI the user's real terminal
    rather than a rendering of one.
    """
    record, result = await _launch(worktree, tmp_path)

    assert harness.launched_argv[0] == record["request"]["provider_executable"]
    assert native_tui_launch.kimi_native_launch.resumes_exactly(harness.launched_argv, SESSION_ID)
    assert result["execution_mode"] == em.NATIVE_TUI
    assert result["terminal_id"] == record["terminal_id"]


@pytest.mark.asyncio
async def test_the_native_launch_starts_no_acp_bridge(
    isolated_memory_db, worktree, tmp_path, harness
):
    """No part of the ACP branch may run under a native reservation.

    Proven from the argv rather than from a flag: the ACP branch's pane
    runs ``-m cli_agent_orchestrator.services.managed_provider_bridge``,
    so its absence from the only argv that started is direct evidence,
    not a restatement of the branch condition.
    """
    await _launch(worktree, tmp_path)

    assert len(harness.terminals) == 1
    assert "cli_agent_orchestrator.services.managed_provider_bridge" not in harness.launched_argv
    assert harness.terminals[0]["protocol_vintage"] == "v2"
    # The TUI is the pane's argv, so nothing is ever typed at a shell.
    assert harness.terminals[0]["initial_message"] is None


@pytest.mark.asyncio
async def test_no_turn_is_submitted_anywhere_in_a_native_launch(
    isolated_memory_db, worktree, tmp_path, harness
):
    """The session the worker inherits must be untouched.

    The bootstrap's job is to mint an id and leave; a turn submitted
    here would land in the transcript the human later reads as their
    own, with no record of who sent it.
    """
    await _launch(worktree, tmp_path)

    assert "session/prompt" not in harness.transport.calls
    assert harness.transport.calls[0] == "initialize"
    assert "session/new" in harness.transport.calls


@pytest.mark.asyncio
async def test_the_bootstrap_runs_the_pinned_binary_in_the_bridge_child_environment(
    isolated_memory_db, worktree, tmp_path, harness
):
    """The minting process and the worker must resolve the same provider.

    A bootstrap that inherited the ambient environment could mint its id
    against a different config, credential, or route than the pane it is
    minting for, and the receipt would still look perfectly consistent.
    """
    record, _ = await _launch(worktree, tmp_path)

    assert harness.bootstrap_kwargs["kimi_binary"] == record["request"]["provider_executable"]
    assert harness.bootstrap_kwargs["working_directory"] == record["working_directory"]
    assert harness.bootstrap_kwargs["env"] == bridge.native_child_environment(
        {
            "provider": record["provider"],
            "model": MODEL,
            "effort": EFFORT,
            "working_directory": record["working_directory"],
            "provider_executable": record["request"]["provider_executable"],
        }
    )


# --------------------------------------------------------------------
# The readiness receipt
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_published_receipt_is_the_native_kind_bind_allows(
    isolated_memory_db, worktree, tmp_path, harness
):
    """``bind_native`` reads readiness from one place in both modes.

    Publishing the same durable record the bridge would have published
    is what keeps bind mode-blind — it validates whatever it finds
    against the mode's allowlist instead of growing a second source of
    truth that could disagree with the first.
    """
    record, _ = await _launch(worktree, tmp_path)
    receipt = _published_receipt(record["reservation_id"])

    assert receipt["provider_receipt_kind"] == "kimi-native-tui-attached"
    assert receipt["provider_receipt_kind"] in v2._NATIVE_TUI_READINESS_RECEIPT_KINDS.values()
    assert receipt["provider_receipt_kind"] not in v2._READINESS_RECEIPT_KINDS.values()
    assert receipt["execution_mode"] == em.NATIVE_TUI
    assert receipt["provider_session_id"] == SESSION_ID
    assert receipt["model_input_ready"] is True


@pytest.mark.asyncio
async def test_the_receipt_quotes_the_argv_that_ran_instead_of_a_transcript(
    isolated_memory_db, worktree, tmp_path, harness
):
    """A native generation has no provider transcript to digest.

    What stands in its place is checkable against the durable attachment
    record — the exact argv digest that started the pane and the process
    identity observed running it — which a self-reported transcript
    digest is not.
    """
    record, _ = await _launch(worktree, tmp_path)
    receipt = _published_receipt(record["reservation_id"])

    assert "provider_transcript_sha256" not in receipt
    assert (
        receipt["launch_argv_sha256"]
        == hashlib.sha256(
            "\x00".join(harness.launched_argv).encode("utf-8", "surrogatepass")
        ).hexdigest()
    )
    assert receipt["process_identity"]
    assert receipt["pane_handle"]


@pytest.mark.asyncio
async def test_the_receipt_reports_the_route_the_provider_read_back(
    isolated_memory_db, worktree, tmp_path, harness
):
    """The route is the read-back value, never the requested one.

    The Kimi resume command line carries no route option, so the only
    window in which a native session's route can be set is while the
    bootstrap still owns it.  Reporting the read-back means a session
    that silently settled elsewhere fails the exact-route check instead
    of being certified by it.
    """
    record, _ = await _launch(worktree, tmp_path)
    receipt = _published_receipt(record["reservation_id"])

    assert receipt["model"] == MODEL
    assert receipt["effort"] == EFFORT
    assert receipt["expected_model"] == MODEL
    assert receipt["expected_effort"] == EFFORT


# --------------------------------------------------------------------
# Nothing unproven gets a pane
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_provider_that_ignores_the_route_never_gets_a_pane(
    isolated_memory_db, worktree, tmp_path, harness
):
    """A silently-wrong route must stop the launch, not decorate it.

    A provider that accepts the set and changes nothing is
    indistinguishable from success unless the value is re-read, and the
    resume line offers no second chance to correct it.
    """
    harness.transport = _FakeAcp(deaf_to_config=True)

    _, result = await _launch(worktree, tmp_path)

    assert result["state"] == "preflight_blocked"
    assert harness.terminals == []


@pytest.mark.asyncio
async def test_a_bootstrap_that_cannot_prove_its_exit_never_gets_a_pane(
    isolated_memory_db, worktree, tmp_path, harness
):
    """A live minter and an attaching TUI are two writers on one session.

    ``reaped: False`` is the transport admitting it does not know the
    process is gone.  Treating "probably exited" as exited is exactly
    the interleaving this ordering exists to prevent.
    """
    harness.transport = _FakeAcp(
        exit_proof={
            "pid": 4242,
            "exit_status": 0,
            "escalation": [boot.STEP_STDIN_CLOSED, boot.STEP_SIGTERM],
            "reaped": False,
        }
    )

    _, result = await _launch(worktree, tmp_path)

    assert result["state"] == "preflight_blocked"
    assert harness.terminals == []


@pytest.mark.asyncio
async def test_a_provider_without_a_native_branch_is_refused_before_any_provider_io(
    isolated_memory_db, worktree, tmp_path, harness, monkeypatch
):
    """Mode support and provider support are separate claims.

    The mode gate admits ``native_tui`` for every provider, so this is
    the gate that stops a provider whose TUI has no resume-by-id
    contract — and it must stop before minting anything, because a
    minted session nobody can resume is a leaked session.
    """

    def _never(**kwargs):
        raise AssertionError("no provider process may start for an unsupported provider")

    monkeypatch.setattr(boot, "StdioAcpBootstrap", _never)
    monkeypatch.setattr(v2, "NATIVE_TUI_PROVIDERS", frozenset())

    _, result = await _launch(worktree, tmp_path)

    assert result["state"] == "preflight_blocked"
    assert harness.terminals == []


@pytest.mark.asyncio
async def test_a_pane_that_resumes_the_wrong_session_is_never_certified(
    isolated_memory_db, worktree, tmp_path, harness, monkeypatch
):
    """A resume that lost its id opens a picker rather than failing.

    So an observed argv that does not resume *exactly* the bound session
    cannot be read as "close enough": the pane may be sitting in another
    session entirely, and a published receipt would certify it.
    """

    def _wrong_session(self):
        return {
            "pane_id": "%7",
            "pid": 4321,
            "start_marker": "Thu Jul 24 10:00:00 2026",
            "argv": [harness.launched_argv[0], "-S", "session_somebody_else"],
        }

    monkeypatch.setattr(v2._V2NativePane, "observe", _wrong_session)

    record, result = await _launch(worktree, tmp_path)

    assert result["state"] == "preflight_blocked"
    with pytest.raises(Exception):
        _published_receipt(record["reservation_id"])
