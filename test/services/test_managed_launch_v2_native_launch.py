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
import json
import os
import subprocess
import tempfile
import uuid
from types import SimpleNamespace
from typing import Any, Mapping, Optional

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.managed_launch_v2 import (
    PROTOCOL_VERSION_V2,
    ManagedLaunchV2BindRequest,
    ManagedLaunchV2ReserveRequest,
)
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import execution_mode as em
from cli_agent_orchestrator.services import installed_bundle_facts as ibf
from cli_agent_orchestrator.services import kimi_native_bootstrap as boot
from cli_agent_orchestrator.services import managed_launch_v2 as v2
from cli_agent_orchestrator.services import managed_provider_bridge as bridge
from cli_agent_orchestrator.services import native_attachment
from cli_agent_orchestrator.services import native_pane_input as npi
from cli_agent_orchestrator.services import native_tui_launch, terminal_service
from cli_agent_orchestrator.services.managed_launch import ManagedLaunchConflict

PINNED_VERSION_BANNER = "kimi 0.29.0"
SESSION_ID = "session_9f2c41ab"
DELIVERY_ID = "44444444-4444-4444-8444-444444444444"
MODEL = "gpt-5.6-sol"
EFFORT = "xhigh"

# Captured at import time, before any fixture patches the module attribute,
# so the COND-0313 preflight-deadline tests can run the *real* banner probe
# against a staged subprocess.
_REAL_PROVIDER_VERSION_BANNER = bridge.provider_version_banner


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
        "agent_profile": "reviewer",
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
        # None means "the pane is where the reservation says"; a test
        # sets this to stage a pane that drifted.
        self.observed_cwd: Optional[str] = None
        # The provider's own reading of its own screen, as a script the
        # launch consumes one entry per look. A cold pane is not idle the
        # instant it exists, so the boot window is staged rather than
        # assumed away -- the last entry repeats once the script runs out,
        # which is what a pane that settles actually does.
        self.pane_status_script: list[TerminalStatus] = [TerminalStatus.IDLE]
        self.pane_reads: list[str] = []

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
        terminal_id = kwargs["reserved_terminal_id"]
        # A real row in the v2 store, because the launch path writes one
        # and later steps read it back. A fake that returned an id without
        # a row would let those steps be tested against a terminal that
        # does not exist — which is the state the fail-closed persistence
        # check exists to catch.
        database.create_terminal_v2(
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

    def _observe(self):
        return {
            "pane_id": "%7",
            "pid": state.observed_pid,
            "start_marker": "Thu Jul 24 10:00:00 2026",
            "argv": state.launched_argv,
            # What a healthy pane reports: the reserved directory, which
            # is also the one the session was minted under.
            "cwd": state.observed_cwd or self._record["working_directory"],
        }

    def _turn_state(pane_id, **_kwargs):
        state.pane_reads.append(pane_id)
        status = (
            state.pane_status_script.pop(0)
            if len(state.pane_status_script) > 1
            else state.pane_status_script[0]
        )
        if isinstance(status, Exception):
            raise status
        return status

    monkeypatch.setattr(boot, "StdioAcpBootstrap", _transport)
    monkeypatch.setattr(bridge, "provider_version_banner", lambda *a, **k: PINNED_VERSION_BANNER)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.create_terminal", _create_terminal
    )
    monkeypatch.setattr(v2._V2NativePane, "observe", _observe)
    monkeypatch.setattr(npi, "observe_kimi_turn_state", _turn_state)
    # Real durations would make every test in this file pay the boot
    # budget. The behaviour under test is the sequence of observations and
    # what the receipt says about them, neither of which is a function of
    # wall-clock length.
    monkeypatch.setattr(v2, "NATIVE_PANE_READY_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(v2, "_NATIVE_PANE_READY_POLL_SECONDS", 0.005)
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
    isolated_memory_db, worktree, tmp_path, harness, monkeypatch
):
    """The pane's own primary process is the provider, not a bridge.

    This is the whole difference between the two modes.  In ACP the pane
    runs the bridge and the provider is its child; here the provider is
    the pane, which is what makes the TUI the user's real terminal
    rather than a rendering of one.
    """
    trust_call = {}

    def _preauthorize(**kwargs):
        trust_call.update(kwargs)
        return str(tmp_path / "workspace-trust-record")

    monkeypatch.setattr(v2.kimi_native_launch, "preauthorize_workspace", _preauthorize)
    record, result = await _launch(worktree, tmp_path)

    assert harness.launched_argv[0] == record["request"]["provider_executable"]
    assert native_tui_launch.kimi_native_launch.resumes_exactly(harness.launched_argv, SESSION_ID)
    assert trust_call == {
        "kimi_home": harness.terminals[0]["env_vars"]["KIMI_CODE_HOME"],
        "working_directory": record["working_directory"],
    }
    assert result["execution_mode"] == em.NATIVE_TUI
    assert result["terminal_id"] == record["terminal_id"]


@pytest.mark.asyncio
async def test_a_slow_but_valid_kimi_version_answer_is_admitted_within_the_provider_bound(
    isolated_memory_db, worktree, tmp_path, harness, monkeypatch
):
    """COND-0313: the native preflight waits out a slow Kimi ``--version``.

    The live failure, under the fixed 5 s deadline this test rejects:
    ``native preflight failed: Command '[.../main.mjs', '--version']' timed
    out after 5.0 seconds`` — recorded against a pinned binary that then
    answered in 0.37–0.41 s four times. One bounded 20 s observation (the
    bound the acceptance harness already allows this exact probe) admits
    the healthy binary; the launch is never replayed.
    """
    executable = str(tmp_path / "fake-kimi")
    observed = {}
    real_run = subprocess.run

    def _run(argv, *args, **kwargs):
        if list(argv) == [executable, "--version"]:
            observed.update(kwargs)
            if kwargs.get("timeout", 0) < 12.0:
                # A cold-starting provider that needs 12 s: the old 5 s
                # bound times out here, the provider bound does not.
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            return SimpleNamespace(returncode=0, stdout=PINNED_VERSION_BANNER + "\n", stderr="")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr("subprocess.run", _run)
    monkeypatch.setattr(bridge, "provider_version_banner", _REAL_PROVIDER_VERSION_BANNER)

    record, result = await _launch(worktree, tmp_path)

    assert observed["timeout"] == 20.0
    assert result["execution_mode"] == em.NATIVE_TUI
    receipt = _published_receipt(record["reservation_id"])
    assert receipt["provider_receipt_kind"] == "kimi-native-tui-attached"


@pytest.mark.asyncio
async def test_a_kimi_version_probe_beyond_the_bound_blocks_before_any_pane_session_or_task(
    isolated_memory_db, worktree, tmp_path, harness, monkeypatch
):
    """Fail closed beyond the provider bound, with zero mutation.

    The blocked record must keep the exact failing command and the
    deadline that fired (credential-redacted), and no pane, provider
    session, or task byte may exist: the bootstrap is never constructed
    and no terminal is created.
    """
    executable = str(tmp_path / "fake-kimi")
    real_run = subprocess.run

    def _run(argv, *args, **kwargs):
        if list(argv) == [executable, "--version"]:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr("subprocess.run", _run)
    monkeypatch.setattr(bridge, "provider_version_banner", _REAL_PROVIDER_VERSION_BANNER)

    _, result = await _launch(worktree, tmp_path)

    assert result["state"] == "preflight_blocked"
    failure = result["preflight_failure"]
    assert failure["reason"] == v2.PREFLIGHT_REASON_NATIVE_PREFLIGHT
    assert failure["task_bytes_submitted"] is False
    # The exact failing command and the deadline that fired, retained.
    assert executable in failure["detail"]
    assert "--version" in failure["detail"]
    assert "timed out after 20.0 seconds" in failure["detail"]
    # Zero pane/session/task mutation.
    assert harness.terminals == []
    assert harness.bootstrap_kwargs == {}


@pytest.mark.asyncio
async def test_a_kimi_build_with_no_session_proof_path_blocks_before_any_pane_session_or_task(
    isolated_memory_db, worktree, tmp_path, harness, monkeypatch
):
    """The 0.36.1 shape: a provable session-proof gap blocks at preflight.

    The installed bundle of the exact build being driven shows the
    argv-erasing title rewrite but not the header labels the rendered
    proof reads, so neither session proof can ever answer and the launch
    could only mint a session, start a pane, and freeze the attachment.
    Post-unpin the mint admits any parseable banner, so the refusal must
    happen here — before the mint, the trust record, and the pane.  An
    *unknown* build is not refused (the 0.32.1/0.33.1 tests above pin
    that): this one is refused because the gap is proven, version-bound,
    from the build's own bytes.
    """
    package = tmp_path / "kimi-package"
    (package / "dist").mkdir(parents=True)
    (package / "dist" / "main.mjs").write_bytes(
        b"function main() {\n\tprocess.title = PROCESS_NAME;\n}\n"
        b'rows = [{label: "Model"}, {label: "Directory"}, {label: "Session"}]\n'
    )
    (package / "package.json").write_text(json.dumps({"version": "9.9.9"}))
    link = tmp_path / "bin" / "kimi"
    link.parent.mkdir(exist_ok=True)
    link.symlink_to(package / "dist" / "main.mjs")
    monkeypatch.setattr(
        ibf.shutil, "which", lambda name: str(link) if os.path.basename(name) == "kimi" else None
    )
    monkeypatch.setattr(bridge, "provider_version_banner", lambda *a, **k: "kimi 9.9.9")

    _, result = await _launch(worktree, tmp_path)

    assert result["state"] == "preflight_blocked"
    failure = result["preflight_failure"]
    assert failure["reason"] == v2.PREFLIGHT_REASON_NATIVE_PREFLIGHT
    assert failure["task_bytes_submitted"] is False
    assert "9.9.9" in failure["detail"]
    # Zero pane/session/task mutation.
    assert harness.terminals == []
    assert harness.bootstrap_kwargs == {}


@pytest.mark.asyncio
async def test_a_kimi_0310_pane_that_rewrote_its_title_is_certified_via_the_rendered_header(
    isolated_memory_db, worktree, tmp_path, harness, monkeypatch
):
    """COND-0312 end to end: a title-rewriting 0.31.0 pane still certifies.

    Kimi Code 0.31.0 rewrites ``process.title`` to ``kimi-code`` after parsing,
    so the kernel argv the observer reads no longer carries the resumed
    ``--session <id>`` -- the live defect that grounded p1-closure.  The launch
    path threads the build's version through to the native launcher, which
    proves the session from the rendered native header instead, and the native
    readiness receipt is published with zero task bytes submitted.
    """
    monkeypatch.setattr(bridge, "provider_version_banner", lambda *a, **k: "kimi 0.31.0")

    rewritten_argv = ["kimi-code", "", "", "", ""]
    header_rows = [
        "│  Welcome to Kimi Code!                                                                              │",
        f"│  Directory: {worktree}                                                                               │",
        f"│  Session:   {SESSION_ID}                                                                             │",
        "│  Model:     K3                                                                                       │",
        "│  Version:   0.31.0                                                                                   │",
    ]

    def _rewritten_observe(self):
        return {
            "pane_id": "%7",
            "pid": harness.observed_pid,
            "start_marker": "Thu Jul 24 10:00:00 2026",
            "argv": list(rewritten_argv),
            "cwd": harness.observed_cwd or self._record["working_directory"],
        }

    monkeypatch.setattr(v2._V2NativePane, "observe", _rewritten_observe)
    monkeypatch.setattr(v2._V2NativePane, "capture_render", lambda self, pane_id: list(header_rows))

    record, result = await _launch(worktree, tmp_path)

    receipt = _published_receipt(record["reservation_id"])
    assert receipt["provider_receipt_kind"] == "kimi-native-tui-attached"
    assert result["execution_mode"] == em.NATIVE_TUI
    assert result["terminal_id"] == record["terminal_id"]


@pytest.mark.asyncio
async def test_a_kimi_0310_pane_that_never_renders_within_the_runway_blocks_with_zero_task_bytes(
    isolated_memory_db, worktree, tmp_path, harness, monkeypatch
):
    """COND-0314, the production r4 shape: freeze before task delivery.

    The live failure ended ``pane_render_does_not_show_bound_session: the
    pane did not render a native header proving session '...' within 15
    seconds`` with ``task_bytes_submitted=false``.  The bound is now the
    shared cold-start runway; a pane that still never proves its session
    inside it must fail the same closed way — blocked before delivery, no
    readiness receipt, the attachment frozen — never admitted, retried, or
    replayed.
    """
    monkeypatch.setattr(bridge, "provider_version_banner", lambda *a, **k: "kimi 0.31.0")
    # A short stand-in for the runway: the deadline path is what is under
    # test here, not its wall-clock length.  The exact value and its boundary
    # behaviour are pinned in the native_tui_launch suite against a fake
    # clock; this seam test must not pay the real 60-second boot budget.
    monkeypatch.setattr(native_tui_launch, "KIMI_RENDER_CONVERGENCE_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(native_tui_launch, "KIMI_RENDER_CONVERGENCE_POLL_SECONDS", 0.01)

    rewritten_argv = ["kimi-code", "", "", "", ""]

    def _rewritten_observe(self):
        return {
            "pane_id": "%7",
            "pid": harness.observed_pid,
            "start_marker": "Thu Jul 24 10:00:00 2026",
            "argv": list(rewritten_argv),
            "cwd": self._record["working_directory"],
        }

    monkeypatch.setattr(v2._V2NativePane, "observe", _rewritten_observe)
    # The pane paints a boot screen forever: the bound session's header
    # never renders inside the runway.
    monkeypatch.setattr(v2._V2NativePane, "capture_render", lambda self, pane_id: ["booting"])

    record, result = await _launch(worktree, tmp_path)

    assert result["state"] == "preflight_blocked"
    failure = result["preflight_failure"]
    assert failure["reason"] == v2.PREFLIGHT_REASON_TUI_LAUNCH_REFUSED
    assert failure["task_bytes_submitted"] is False
    assert "pane_render_does_not_show_bound_session" in failure["detail"]
    assert "did not render" in failure["detail"]
    # Zero delivery anywhere: no readiness receipt, no provider turn, and
    # the attachment frozen against any later claim.
    with pytest.raises(Exception):
        _published_receipt(record["reservation_id"])
    assert "session/prompt" not in harness.transport.calls
    attachment = native_attachment.get("kimi_cli", SESSION_ID)
    assert attachment is not None
    assert attachment["state"] == native_attachment.AMBIGUOUS
    assert attachment["ambiguity_reason"] == "pane_render_does_not_show_bound_session"


@pytest.mark.asyncio
async def test_a_kimi_0320_pane_that_rewrote_its_title_is_certified_via_the_rendered_header(
    isolated_memory_db, worktree, tmp_path, harness, monkeypatch
):
    """COND-0315 end to end: the stage-verified 0.32.0 build certifies.

    The installed 0.32.0 keeps the 0.31.0 title rewrite, so the kernel argv
    still reads ``['kimi-code', '', ...]`` and the resumed session is proven
    from the rendered native header, which names the bound session, the
    0.32.0 version line, and the bound worktree.  Before the stage
    attestation this exact launch failed closed at the bootstrap --
    ``kimi version drift: accepted [...], installed '0.32.0'`` (run
    cond-0303-pr74-review-k3-r5, zero task bytes) -- and it must now publish
    the native readiness receipt the same way 0.31.0 does.
    """
    monkeypatch.setattr(bridge, "provider_version_banner", lambda *a, **k: "kimi 0.32.0")

    rewritten_argv = ["kimi-code", "", "", "", ""]
    header_rows = [
        "│  Welcome to Kimi Code!                                                                              │",
        f"│  Directory: {worktree}                                                                               │",
        f"│  Session:   {SESSION_ID}                                                                             │",
        "│  Model:     K3                                                                                       │",
        "│  Version:   0.32.0                                                                                   │",
    ]

    def _rewritten_observe(self):
        return {
            "pane_id": "%7",
            "pid": harness.observed_pid,
            "start_marker": "Thu Jul 24 10:00:00 2026",
            "argv": list(rewritten_argv),
            "cwd": harness.observed_cwd or self._record["working_directory"],
        }

    monkeypatch.setattr(v2._V2NativePane, "observe", _rewritten_observe)
    monkeypatch.setattr(v2._V2NativePane, "capture_render", lambda self, pane_id: list(header_rows))

    record, result = await _launch(worktree, tmp_path)

    receipt = _published_receipt(record["reservation_id"])
    assert receipt["provider_receipt_kind"] == "kimi-native-tui-attached"
    # The receipt records the provider's banner verbatim, and the bind-time
    # check normalizes it against the accepted set.
    assert receipt["provider_version"] == "kimi 0.32.0"
    assert result["execution_mode"] == em.NATIVE_TUI
    assert result["terminal_id"] == record["terminal_id"]
    assert "session/prompt" not in harness.transport.calls


@pytest.mark.asyncio
async def test_a_kimi_0321_banner_launches_and_proves_itself_at_runtime(
    isolated_memory_db, worktree, tmp_path, harness, monkeypatch
):
    """An unlisted neighbour of 0.32.0 launches under the unpinned policy.

    The bootstrap's ACP exchange against the installed binary is itself the
    identity proof — an unlisted build runs it rather than being refused on
    a missing table row.  The harness's fake transport honors the exchange,
    the pane's observed argv binds the minted session, and the launch
    publishes its receipt with the banner recorded verbatim.
    """
    monkeypatch.setattr(bridge, "provider_version_banner", lambda *a, **k: "kimi 0.32.1")

    record, result = await _launch(worktree, tmp_path)

    receipt = _published_receipt(record["reservation_id"])
    assert receipt["provider_receipt_kind"] == "kimi-native-tui-attached"
    assert receipt["provider_version"] == "kimi 0.32.1"
    assert result["execution_mode"] == em.NATIVE_TUI
    assert "session/prompt" not in harness.transport.calls


@pytest.mark.asyncio
async def test_a_kimi_0330_pane_is_certified_via_the_rendered_header_on_the_real_geometry(
    isolated_memory_db, worktree, tmp_path, harness, monkeypatch
):
    """COND-0315 end to end: the stage-verified 0.33.0 build certifies.

    The current pin.  The header rows below are the shape a real
    ``capture-pane`` paints on 0.33.0: the one-cell ``GutterContainer``
    left pad before every box vertical, and an ``MCP:`` row after the four
    proof labels when servers connect.  The launch must publish the native
    readiness receipt against that real geometry, with zero task bytes.
    """
    monkeypatch.setattr(bridge, "provider_version_banner", lambda *a, **k: "kimi 0.33.0")

    rewritten_argv = ["kimi-code", "", "", "", ""]
    header_rows = [
        " ╭────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╮",
        " │  ▐█▛█▛█▌  Welcome to Kimi Code!                                                                                                  │",
        " │  ▐█████▌  Send /help for help information.                                                                                       │",
        " │                                                                                                                                  │",
        f" │  Directory: {worktree}                                                                                                           │",
        f" │  Session:   {SESSION_ID}                                                                                                         │",
        " │  Model:     K3                                                                                                                   │",
        " │  Version:   0.33.0                                                                                                               │",
        " │  MCP:       5 connected                                                                                                          │",
        " │                                                                                                                                  │",
        " ╰────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯",
    ]

    def _rewritten_observe(self):
        return {
            "pane_id": "%7",
            "pid": harness.observed_pid,
            "start_marker": "Thu Jul 24 10:00:00 2026",
            "argv": list(rewritten_argv),
            "cwd": harness.observed_cwd or self._record["working_directory"],
        }

    monkeypatch.setattr(v2._V2NativePane, "observe", _rewritten_observe)
    monkeypatch.setattr(v2._V2NativePane, "capture_render", lambda self, pane_id: list(header_rows))

    record, result = await _launch(worktree, tmp_path)

    receipt = _published_receipt(record["reservation_id"])
    assert receipt["provider_receipt_kind"] == "kimi-native-tui-attached"
    assert receipt["provider_version"] == "kimi 0.33.0"
    assert result["execution_mode"] == em.NATIVE_TUI
    assert result["terminal_id"] == record["terminal_id"]
    assert "session/prompt" not in harness.transport.calls


@pytest.mark.asyncio
async def test_a_kimi_0331_banner_launches_and_proves_itself_at_runtime(
    isolated_memory_db, worktree, tmp_path, harness, monkeypatch
):
    """The build beyond the current pin launches under the unpinned policy.

    Unlisted is merely nothing written down: the bootstrap exchange runs
    against the installed binary and proves the identity contract at
    runtime, and the observed pane argv binds the minted session.  A failed
    observation — an unparseable banner — would still refuse before any
    pane, provider session, or task byte.
    """
    monkeypatch.setattr(bridge, "provider_version_banner", lambda *a, **k: "kimi 0.33.1")

    record, result = await _launch(worktree, tmp_path)

    receipt = _published_receipt(record["reservation_id"])
    assert receipt["provider_receipt_kind"] == "kimi-native-tui-attached"
    assert receipt["provider_version"] == "kimi 0.33.1"
    assert result["execution_mode"] == em.NATIVE_TUI
    assert "session/prompt" not in harness.transport.calls


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
    expected_environment = bridge.native_child_environment(
        {
            "provider": record["provider"],
            "model": MODEL,
            "effort": EFFORT,
            "working_directory": record["working_directory"],
            "provider_executable": record["request"]["provider_executable"],
        }
    )
    actual_environment = dict(harness.bootstrap_kwargs["env"])
    private_home = actual_environment.pop("KIMI_CODE_HOME")
    assert actual_environment == expected_environment
    assert private_home.startswith(
        str(v2.COMPANION_DIR / record["terminal_id"] / record["generation"])
    )
    assert harness.terminals[0]["env_vars"]["KIMI_CODE_HOME"] == private_home


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
            "cwd": self._record["working_directory"],
        }

    monkeypatch.setattr(v2._V2NativePane, "observe", _wrong_session)

    record, result = await _launch(worktree, tmp_path)

    assert result["state"] == "preflight_blocked"
    with pytest.raises(Exception):
        _published_receipt(record["reservation_id"])


@pytest.mark.asyncio
async def test_missing_glm_marker_tears_down_published_terminal_and_freezes_attachment(
    isolated_memory_db, monkeypatch
):
    """A post-publication route refusal leaves no live pane to recover."""
    record = {
        "provider": "claude_code",
        "terminal_id": "terminal-marker-missing",
        "generation": "generation-marker-missing",
        "session_name": "cao-marker-missing",
    }
    bootstrap = {"native_session_id": "native-marker-missing"}
    native_attachment.declare(
        provider=record["provider"],
        native_session_id=bootstrap["native_session_id"],
        terminal_id=record["terminal_id"],
        generation=record["generation"],
        execution_mode=em.NATIVE_TUI,
        intent=native_attachment.acquire_intent(
            acquisition_method=native_attachment.ACQUISITION_CHOSEN_SESSION_ID,
            acquisition_receipt={"session_id": bootstrap["native_session_id"]},
            admits_only_new_instructions=True,
            replays_task_bytes=False,
        ),
    )
    live_pane = {"present": True}
    deletion = {}

    def _delete_terminal(terminal_id, *, registry, expected_generation, expected_session):
        deletion.update(
            terminal_id=terminal_id,
            registry=registry,
            expected_generation=expected_generation,
            expected_session=expected_session,
        )
        live_pane["present"] = False
        return True

    monkeypatch.setattr(terminal_service, "delete_terminal", _delete_terminal)
    registry = object()

    cleanup_error = await v2._teardown_published_native_terminal(
        record=record,
        bootstrap=bootstrap,
        registry=registry,
        reason="glm_route_consumed_marker_missing",
    )

    assert cleanup_error is None
    assert live_pane["present"] is False
    assert deletion == {
        "terminal_id": record["terminal_id"],
        "registry": registry,
        "expected_generation": record["generation"],
        "expected_session": record["session_name"],
    }
    attachment = native_attachment.get(record["provider"], bootstrap["native_session_id"])
    assert attachment["state"] == native_attachment.AMBIGUOUS
    assert attachment["ambiguity_reason"] == "glm_route_consumed_marker_missing"


# --------------------------------------------------------------------
# The working directory the session is filed under
# --------------------------------------------------------------------
#
# The provider files a session under the working-directory *string* it
# is given and resolves a later resume against that same string, while
# the TUI that resume starts reports only the realpath.  So two names
# for one physical directory are two different sessions to the provider,
# and a launch that mixes them succeeds at every checkpoint and then
# produces a pane that exits about a second later.  The cases below
# cover each boundary that now refuses the mix, and prove that refusing
# is all any of them does — no path is silently corrected, because a
# correction applied at one boundary and not the others *is* the mix.


def test_a_symlinked_temporary_root_is_refused_by_name(isolated_memory_db, worktree, tmp_path):
    """The exact shape a live rig produced: ``/tmp`` reached as itself.

    On macOS ``/tmp`` is a symlink to ``/private/tmp``, so this is one
    directory under two names.  The refusal must name the other one,
    because a caller told only "not canonical" has to go and work out
    what to send instead.
    """
    alias = "/tmp/cao-canonicality-probe"
    canonical = os.path.realpath(alias)
    if alias == canonical:
        pytest.skip("this platform's /tmp is already canonical")
    os.makedirs(canonical, exist_ok=True)

    with pytest.raises(v2.ManagedLaunchConflict) as refusal:
        v2.reserve(_reserve_request(worktree, tmp_path, working_directory=alias))

    assert canonical in str(refusal.value)
    assert alias in str(refusal.value)


def test_a_reservation_refused_for_its_directory_leaves_no_row(
    isolated_memory_db, worktree, tmp_path
):
    """Refused before anything durable exists, not rolled back after.

    The distinction matters to a caller retrying with the corrected
    path: a half-created reservation would make the corrected retry
    collide with the refused attempt's own id.

    The non-canonical directory is built explicitly rather than taken
    from ``tempfile.mkdtemp``, whose root is symlinked on macOS but not
    on Linux.  What is under test here is that a refusal leaves no row,
    which is true on every platform, so the test should run on every
    platform instead of skipping where the shortcut stops working.
    """
    real = tmp_path / "refused-real"
    real.mkdir()
    (tmp_path / "refused-alias").symlink_to(real)
    request = _reserve_request(
        worktree, tmp_path, working_directory=str(tmp_path / "refused-alias")
    )
    reservation_id = request.reservation_id

    with pytest.raises(v2.ManagedLaunchConflict):
        v2.reserve(request)

    # No row, so the id is still free -- and the proof is that reserving
    # it again with a good directory succeeds rather than conflicting.
    with pytest.raises(v2.ManagedLaunchNotFound):
        v2.get(reservation_id)
    record, created = v2.reserve(
        _reserve_request(worktree, tmp_path, reservation_id=reservation_id)
    )
    assert created is True
    assert record["reservation_id"] == reservation_id


def test_a_mkdtemp_directory_is_refused_because_it_is_never_canonical(
    isolated_memory_db, worktree, tmp_path
):
    """The reachable production case, not a contrived one.

    ``tempfile.mkdtemp`` returns a path under a symlinked root on macOS
    every single time, and this codebase already builds provider
    working directories that way.  Skipped rather than faked where the
    platform makes it canonical, so the test never passes by accident.
    """
    created_directory = tempfile.mkdtemp()
    if created_directory == os.path.realpath(created_directory):
        pytest.skip("this platform's temporary root is already canonical")

    with pytest.raises(v2.ManagedLaunchConflict) as refusal:
        v2.reserve(_reserve_request(worktree, tmp_path, working_directory=created_directory))

    assert os.path.realpath(created_directory) in str(refusal.value)


def test_a_symlinked_interior_component_is_refused(isolated_memory_db, worktree, tmp_path):
    """A path can be absolute, existing, and still not canonical.

    The link is in the middle rather than at the root, which is the case
    a check written as "does it start with /private" would wave through.
    """
    real = tmp_path / "real"
    real.mkdir()
    (real / "inner").mkdir()
    (tmp_path / "link").symlink_to(real)
    through_link = str(tmp_path / "link" / "inner")

    with pytest.raises(v2.ManagedLaunchConflict) as refusal:
        v2.reserve(_reserve_request(worktree, tmp_path, working_directory=through_link))

    assert str(real / "inner") in str(refusal.value)


def test_a_nonexistent_directory_is_refused_as_missing_rather_than_as_uncanonical(
    isolated_memory_db, worktree, tmp_path
):
    """Two different faults must not share one message.

    ``realpath`` happily resolves a path that does not exist, so a
    caller who mistyped a directory and a caller who sent a symlinked
    one would otherwise both be told to send the canonical form -- and
    only one of them has one to send.
    """
    missing = str(tmp_path / "not-there")

    with pytest.raises(v2.ManagedLaunchConflict) as refusal:
        v2.reserve(_reserve_request(worktree, tmp_path, working_directory=missing))

    assert "existing directory" in str(refusal.value)


def test_a_replayed_reservation_echoes_the_request_byte_for_byte(
    isolated_memory_db, worktree, tmp_path
):
    """Nothing on this path rewrites the stored request or the echo.

    A caller that replays a reserve compares what comes back against
    what it sent.  Had the directory been normalised on ingest instead
    of refused, a caller sending one valid spelling would read back
    another and see its own ordinary retry as a conflict -- which is
    exactly why every check here refuses rather than corrects.
    """
    request = _reserve_request(worktree, tmp_path)

    first, created = v2.reserve(request)
    second, created_again = v2.reserve(request)

    assert created is True and created_again is False
    assert first == second
    assert first["working_directory"] == request.working_directory
    assert first["request"]["working_directory"] == request.working_directory
    # The stored request bytes, not a re-render of them: identical
    # across the replay and still carrying the caller's own spelling.
    stored = v2._canonical_json(first["request"])
    assert v2._canonical_json(second["request"]) == stored
    assert request.working_directory in stored


@pytest.mark.asyncio
async def test_a_canonical_directory_launches_and_consumes_no_turn(
    isolated_memory_db, worktree, tmp_path, harness
):
    """The accepting case, proven to still cost nothing at the provider.

    A check that refuses everything would pass all the cases above.
    This one runs the whole reserve-mint-launch chain on a canonical
    directory and asserts the directory reached all three boundaries as
    the same string, with no turn submitted anywhere.
    """
    record, result = await _launch(worktree, tmp_path)

    assert result["state"] != "preflight_blocked"
    assert record["working_directory"] == os.path.realpath(record["working_directory"])
    # One string at the reservation, the mint, and the pane.
    assert harness.bootstrap_kwargs["working_directory"] == record["working_directory"]
    assert harness.terminals[-1]["working_directory"] == record["working_directory"]
    assert "session/prompt" not in harness.transport.calls
    assert _published_receipt(record["reservation_id"])["working_directory"] == (
        record["working_directory"]
    )


@pytest.mark.asyncio
async def test_a_pane_running_in_another_directory_is_never_certified(
    isolated_memory_db, worktree, tmp_path, harness
):
    """The late check: the pane exists, but not where the session does.

    Everything before this point succeeded -- the directory was
    canonical at the reservation, at the mint, and at the launch -- and
    the pane still came up somewhere else.  It resumes the right session
    id in a directory that session was not filed under, so the provider
    will refuse to open it.  Caught before the attachment is published,
    which is what keeps the generation from ever becoming bindable.
    """
    harness.observed_cwd = os.path.realpath(tempfile.gettempdir())
    assert harness.observed_cwd != str(worktree)

    record, result = await _launch(worktree, tmp_path)

    assert result["state"] == "preflight_blocked"
    # Frozen with the reason that names this boundary, so a later
    # reconciler is sent to the pane's directory rather than to its argv.
    frozen = native_attachment.get("kimi_cli", SESSION_ID)
    assert frozen["state"] == native_attachment.AMBIGUOUS
    assert frozen["ambiguity_reason"] == native_tui_launch.AMBIGUOUS_PANE_WORKDIR_MISMATCH
    # No readiness receipt, so bind has nothing to read and the
    # generation cannot be admitted into.
    with pytest.raises(Exception):
        _published_receipt(record["reservation_id"])
    # And the pane was never typed at: a native launch types nothing by
    # construction, and a blocked one must not have found an exception.
    assert harness.terminals[-1]["initial_message"] is None


# --------------------------------------------------------------------
# The boot window: a pane that exists is not a pane that accepts input
# --------------------------------------------------------------------


def _bind_request(record) -> ManagedLaunchV2BindRequest:
    return ManagedLaunchV2BindRequest(
        protocol_version=PROTOCOL_VERSION_V2,
        terminal_id=record["terminal_id"],
        generation=record["generation"],
        attempt_id=str(uuid.uuid4()),
    )


@pytest.mark.asyncio
async def test_launch_waits_out_the_boot_window_before_certifying_readiness(
    isolated_memory_db, worktree, tmp_path, harness
):
    """The cold start this whole seam exists for, staged exactly.

    A real cold pane walks ``UNKNOWN`` (no TUI chrome painted yet) into
    ``PROCESSING`` (connecting its servers) and only then into ``IDLE``
    (a composer that accepts input).  A launch that published readiness
    on the strength of the process existing would certify the first of
    those, and the task admitted against it would be typed into a boot
    screen that swallows it silently.

    The assertion is therefore about *when* readiness was declared, not
    merely that it was: the pane had to be looked at until it said so.
    """
    harness.pane_status_script = [
        TerminalStatus.UNKNOWN,
        TerminalStatus.PROCESSING,
        TerminalStatus.PROCESSING,
        TerminalStatus.IDLE,
    ]

    record, result = await _launch(worktree, tmp_path)

    assert result["state"] == "launching"
    receipt = _published_receipt(record["reservation_id"])
    assert receipt["model_input_ready"] is True
    # Four looks at the one bound pane: the three that said "not yet" and
    # the one that said "now". A wait that had inferred readiness from
    # elapsed time instead would show one.
    assert harness.pane_reads == ["%7"] * 4
    observed = receipt["model_input_ready_observation"]
    assert observed["input_ready"] is True
    assert observed["provider_status"] == TerminalStatus.IDLE.value
    assert observed["pane_id"] == "%7"
    # Named so a refusal at admission and this receipt can be checked
    # against each other rather than each being taken on trust.
    assert observed["authority"] == "observe_kimi_turn_state"


@pytest.mark.asyncio
async def test_a_pane_ready_on_the_first_look_is_certified_without_waiting(
    isolated_memory_db, worktree, tmp_path, harness
):
    """The other cold launch: readiness that is already true is not delayed.

    The bound exists to cap a boot, not to impose one.  A launch that
    slept out its budget regardless would make every generation pay for
    the slowest possible provider start.
    """
    harness.pane_status_script = [TerminalStatus.IDLE]

    record, _ = await _launch(worktree, tmp_path)

    assert _published_receipt(record["reservation_id"])["model_input_ready"] is True
    assert harness.pane_reads == ["%7"]


@pytest.mark.asyncio
async def test_a_completed_pane_is_also_input_ready(
    isolated_memory_db, worktree, tmp_path, harness
):
    """A settled response leaves the composer just as writable as fresh idle."""
    harness.pane_status_script = [TerminalStatus.COMPLETED]

    record, _ = await _launch(worktree, tmp_path)

    receipt = _published_receipt(record["reservation_id"])
    assert receipt["model_input_ready"] is True
    assert receipt["model_input_ready_observation"]["provider_status"] == (
        TerminalStatus.COMPLETED.value
    )
    assert harness.pane_reads == ["%7"]


@pytest.mark.asyncio
async def test_a_pane_that_never_becomes_ready_is_reported_unready_and_bind_refuses(
    isolated_memory_db, worktree, tmp_path, harness
):
    """The receipt states what was seen, and the existing gate does the rest.

    ``model_input_ready`` was a hardcoded ``True`` for the whole life of
    this path, which made ``bind_native``'s check of it unreachable --
    a gate that could never fire against a generation that was never
    ready.  Making the field an observation is what turns that check back
    on, so no new refusal had to be invented for this case.
    """
    harness.pane_status_script = [TerminalStatus.PROCESSING]

    record, result = await _launch(worktree, tmp_path)

    # The launch itself does not fail: a pane that is still booting is a
    # fact to report, not an error to hide.
    assert result["state"] == "launching"
    receipt = _published_receipt(record["reservation_id"])
    assert receipt["model_input_ready"] is False
    assert receipt["model_input_ready_observation"]["provider_status"] == (
        TerminalStatus.PROCESSING.value
    )
    # It was looked at repeatedly rather than written off after one read.
    assert len(harness.pane_reads) > 1

    with pytest.raises(ManagedLaunchConflict, match="model_input_ready"):
        v2.bind_native(record["reservation_id"], _bind_request(record))

    # Fail-closed: no binding, so no admission can name this generation.
    assert v2.get(record["reservation_id"])["state"] == "launching"


@pytest.mark.asyncio
async def test_a_pane_that_cannot_be_read_is_never_certified_as_ready(
    isolated_memory_db, worktree, tmp_path, harness
):
    """ "We could not look" is not "we looked and it was fine".

    An unreadable pane produces no status at all, and the receipt says
    so -- an observation field left at ``None`` rather than filled in
    with the reading that was never taken.
    """
    harness.pane_status_script = [npi.NativePaneInputUnavailable("tmux is not answering")]

    record, _ = await _launch(worktree, tmp_path)

    receipt = _published_receipt(record["reservation_id"])
    assert receipt["model_input_ready"] is False
    observed = receipt["model_input_ready_observation"]
    assert observed["provider_status"] is None
    assert "tmux is not answering" in observed["detail"]


# --------------------------------------------------------------------
# The proven native session reaches the durable row
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_proven_native_session_is_written_to_the_terminal_row(
    isolated_memory_db, worktree, tmp_path, harness
):
    """Persisted on the real launch path, not only by a setter under test.

    Before this, ``native_session_id`` was NULL on every managed row, which
    silently costs the native-session half of the supersession test: a pane
    later running a *different* session looks identical to the right one,
    because the comparison has nothing to compare.
    """
    record, _ = await _launch(worktree, tmp_path)

    row = database.get_terminal_metadata_v2(record["terminal_id"])
    assert row["v2_native_session_id"] == SESSION_ID
    # And it reaches the surface the conductor reads.
    assert v2._published_terminal_facts(record["terminal_id"])["native_session_id"] == SESSION_ID


@pytest.mark.asyncio
async def test_a_session_that_cannot_be_recorded_blocks_instead_of_certifying(
    isolated_memory_db, worktree, tmp_path, harness, monkeypatch
):
    """Fail closed: no readiness receipt for a row that cannot name its session.

    Zero task bytes have been submitted by this point, so refusing costs a
    finalizable reservation rather than a lost turn — while publishing
    anyway would certify a pane nothing can later prove the identity of.
    """
    monkeypatch.setattr(database, "set_terminal_v2_native_session_id", lambda *_a, **_k: False)

    record, result = await _launch(worktree, tmp_path)

    assert result["state"] == "preflight_blocked"
    failure = result["preflight_failure"]
    assert failure["reason"] == v2.PREFLIGHT_REASON_READINESS
    assert "could not be durably recorded" in failure["detail"]
    # The zero-byte claim is what makes this finalizable.
    assert failure["task_bytes_submitted"] is False
    # No readiness was published, so nothing downstream can bind it.
    state = bridge.read_state(record["reservation_id"])
    assert (state or {}).get("state") != "ready"


@pytest.mark.asyncio
async def test_the_session_is_recorded_before_the_readiness_receipt(
    isolated_memory_db, worktree, tmp_path, harness, monkeypatch
):
    """Ordering is the guarantee: a published receipt implies a recorded session.

    A consumer that sees ``ready`` may act on the row immediately, so the
    row has to be complete first — the reverse order would leave a window
    in which readiness is public and the identity is not.
    """
    order: list[str] = []
    real_setter = database.set_terminal_v2_native_session_id
    real_publish = bridge.publish_native_ready_state

    def _setter(terminal_id, native_session_id):
        order.append("recorded")
        return real_setter(terminal_id, native_session_id)

    def _publish(reservation_id, receipt):
        order.append("published")
        return real_publish(reservation_id, receipt)

    monkeypatch.setattr(database, "set_terminal_v2_native_session_id", _setter)
    monkeypatch.setattr(bridge, "publish_native_ready_state", _publish)

    await _launch(worktree, tmp_path)

    assert order == ["recorded", "published"]
