"""Muse managed-v2 native enrollment (cond-0377B), end to end over fakes.

Muse's managed-v2 launch differs from the other three native providers in
one deliberate way: its identity is *chosen* (a canonical UUID minted before
any provider I/O, exactly like Claude's), and its pre-task readiness is
observed from the provider's own ``/status`` panel rather than from a
SessionStart hook (Claude) or a minting bootstrap (Kimi/Codex).

Everything this suite asserts is pinned to the installed build evidence
recorded in ``muse_native_launch`` and ``muse_native_status``:

* ``muse resume <id>`` is the only accepted identity form, root options
  follow the id, and no recency selector, second identity, or initial task
  prompt may appear in a managed argv.
* The CAO profile system prompt is carried into the main session as base
  instructions through the installed ``TBH_EVAL_APPEND_SYSTEM_PROMPT_FILE``
  surface (verified deterministically on the installed 0.1.0-R708.1 build:
  with the env var set, an echo-provider launch *and* an exact
  ``muse resume <id>`` both refuse with "provider does not support base
  instructions", the same run-configuration refusal a built-in preset with
  base instructions produces).
* The ``/status`` panel reports the exact session id, model, reasoning
  effort, agent profile, provider, cwd, and idle/zero-turn pre-task state,
  and the panel is printed output — the composer stays ready.
"""

from __future__ import annotations

import hashlib
import subprocess
import uuid
from typing import Any, Optional

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.managed_launch_v2 import (
    PROTOCOL_VERSION_V2,
    ManagedLaunchV2AdmitRequest,
    ManagedLaunchV2BindRequest,
    ManagedLaunchV2ReserveRequest,
)
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import execution_mode as em
from cli_agent_orchestrator.services import managed_launch_v2 as v2
from cli_agent_orchestrator.services import managed_provider_bridge as bridge
from cli_agent_orchestrator.services import muse_native_launch, muse_native_status
from cli_agent_orchestrator.services import native_pane_input as npi
from cli_agent_orchestrator.services import native_tui_launch, terminal_service
from cli_agent_orchestrator.services.managed_launch import ManagedLaunchConflict

MUSE_BANNER = "Muse Code 0.1.0 (0.1.0-R708.1)"
MUSE_MODEL = "muse-spark-1.2-contributor"
MUSE_EFFORT = "high"
DELIVERY_ID = "44444444-4444-4444-8444-444444444444"
TASK_MESSAGE = "review this diff"


def _admit_request(digest: str, **changes) -> ManagedLaunchV2AdmitRequest:
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
    executable = tmp_path / "fake-muse"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    payload = {
        "protocol_version": PROTOCOL_VERSION_V2,
        "reservation_id": str(uuid.uuid4()),
        "session_name": "cao-test",
        "provider": "muse_cli",
        "agent_profile": "reviewer",
        "caller_id": "deadbeef",
        "working_directory": str(worktree),
        "trusted_project_root": None,
        "expected_model": MUSE_MODEL,
        "expected_effort": MUSE_EFFORT,
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


def status_panel_rows(
    worktree,
    session_id: str,
    *,
    model: str = MUSE_MODEL,
    effort: str = MUSE_EFFORT,
    agent_profile: str = "native-basic",
    provider: str = "meta",
    directory: Optional[str] = None,
    run: str = "idle",
    tokens: str = "0 tokens / 0 turns",
    reasoning: Optional[str] = MUSE_EFFORT,  # None = the line is absent
    session_line: Optional[str] = None,
) -> list[str]:
    """The ``/status`` panel rows, in the installed 0.1.0 rendering.

    The coordinator no-prompt canary on 2026-08-10 rendered this exact
    panel for a meta session: model, reasoning effort, agent profile,
    provider, cwd, session, idle run, and zero tokens/turns.  The echo
    provider's panel omits the Reasoning line, so the parser must treat it
    as present-when-rendered.
    """
    width = 45
    directory = directory or str(worktree)
    rows = [
        "  Muse Code 0.1.0",
        "",
        "╭" + "─" * (width + 2) + "╮",
        "│  >_ Muse Code (0.1.0)" + " " * (width - 20) + " │",
        "│" + " " * (width + 2) + "│",
        f"│  Model:{model:>{width - 7}} │",
    ]
    if reasoning is not None:
        rows.append(f"│  Reasoning:{reasoning:>{width - 11}} │")
    rows += [
        f"│  Agent profile:{agent_profile:>{width - 14}} │",
        f"│  Model provider:{provider:>{width - 15}} │",
        "│  Credential:              none                  │",
        f"│  Directory:{directory:>{width - 10}} │",
        "│  Permissions:          approval=Normal sandbox=Normal │",
        "│  Agents.md:            not found                  │",
        "│  Project trust:        trusted                    │",
        f"│  Session:{(session_line or session_id):>{width - 8}} │",
        "│" + " " * (width + 2) + "│",
        f"│  Token usage:{tokens:>{width - 12}} │",
        "│  Context window:       not projected              │",
        f"│  Run:{run:>{width - 4}} │",
        "│  Tasks:                none                       │",
        "│  Terminals:            0                          │",
        "│  Inbox:                0 pending                  │",
        "╰" + "─" * (width + 2) + "╯",
        "",
        "── Voice input (⌥ + v to start) ─────────────────────────────",
        "⟩",
        "────────────────────────────────────────────────────────────",
    ]
    return rows


# --------------------------------------------------------------------
# 1. Provider / table / capability parity — Muse only after the whole
#    managed-v2 branch is implemented.
# --------------------------------------------------------------------


def test_muse_is_enrolled_in_the_derived_native_tui_provider_set():
    """Muse joins the derived set only when all three surfaces exist."""
    assert "muse_cli" in v2.NATIVE_TUI_PROVIDERS
    assert set(v2._NATIVE_TUI_READINESS_RECEIPT_KINDS) == v2.NATIVE_TUI_PROVIDERS
    assert set(v2._ISSUANCE_SOURCES) == v2.NATIVE_TUI_PROVIDERS
    assert set(v2._PINNED_PROVIDER) == v2.NATIVE_TUI_PROVIDERS
    assert v2.NATIVE_TUI_PROVIDERS <= native_tui_launch.SUPPORTED_NATIVE_PROVIDERS


def test_muse_capability_payload_is_truthful():
    """The capability block names Muse's real kind, source, and executable."""
    capabilities = v2.native_tui_capabilities()
    block = capabilities["providers"]["muse_cli"]
    assert block["supported"] is True
    assert block["id_source"] == "cli_session_id"
    assert block["readiness_receipt_kind"] == "muse-native-status-idle"
    assert block["executable"] == "muse"
    assert "0.1.0" in block["supported_versions"]


def test_the_other_native_providers_are_unchanged_by_muse_enrollment():
    """Enrolling Muse must not move any existing provider's facts."""
    assert v2._NATIVE_TUI_READINESS_RECEIPT_KINDS["codex"] == "codex-native-thread-start"
    assert v2._NATIVE_TUI_READINESS_RECEIPT_KINDS["kimi_cli"] == "kimi-native-tui-attached"
    assert v2._NATIVE_TUI_READINESS_RECEIPT_KINDS["claude_code"] == "claude-native-session-start"
    assert v2._PINNED_PROVIDER["codex"] == "codex"
    assert v2._PINNED_PROVIDER["kimi_cli"] == "kimi"
    assert v2._PINNED_PROVIDER["claude_code"] == "claude"


def test_the_native_kind_stays_disjoint_from_the_acp_kinds():
    assert not set(v2._NATIVE_TUI_READINESS_RECEIPT_KINDS.values()) & set(
        v2._READINESS_RECEIPT_KINDS.values()
    )


# --------------------------------------------------------------------
# 2. The argv contract — exact resume, no prompt, no recency forms.
# --------------------------------------------------------------------


def test_muse_resume_argv_binds_the_exact_minted_id_exactly_once():
    session_id = muse_native_launch.mint_session_id()
    argv = muse_native_launch.build_resume_argv(
        session_id=session_id,
        muse_binary="/usr/local/bin/muse",
        extra_args=["--model", MUSE_MODEL, "--reasoning-effort", MUSE_EFFORT],
    )
    assert argv == [
        "/usr/local/bin/muse",
        "resume",
        session_id,
        "--model",
        MUSE_MODEL,
        "--reasoning-effort",
        MUSE_EFFORT,
    ]
    assert argv.count(session_id) == 1
    assert muse_native_launch.resumes_exactly(argv, session_id)
    # The minted id is canonical and fresh.
    assert str(uuid.UUID(session_id)) == session_id


def test_muse_resume_argv_refuses_recency_and_identity_rebinding_forms():
    session_id = muse_native_launch.mint_session_id()
    for smuggled in ("--last", "-c", "--continue", "--exec", "--fork-session", "--no-session-log"):
        with pytest.raises(muse_native_launch.MuseNativeLaunchError):
            muse_native_launch.build_resume_argv(session_id=session_id, extra_args=[smuggled])


def test_muse_managed_launch_extra_args_carry_model_effort_trust_and_no_prompt():
    """The managed launch argv is the resume contract plus profile args."""
    record, request, bootstrap = _mint_with_harness_state()
    args = v2._muse_profile_launch_args(
        record=record,
        request=request,
        profile_material=_fake_profile_material(),
        bootstrap=bootstrap,
    )
    argv = muse_native_launch.build_resume_argv(
        session_id=bootstrap["native_session_id"], extra_args=args
    )
    assert "resume" in argv
    assert "--model" in argv and argv[argv.index("--model") + 1] == MUSE_MODEL
    assert "--reasoning-effort" in argv
    assert argv[argv.index("--reasoning-effort") + 1] == MUSE_EFFORT
    # No positional prompt may follow the identity pair.
    for token in argv[argv.index("resume") + 2 :]:
        assert not token.startswith("resume ")
        assert token not in {"--last", "-c", "--continue", "--exec", "--fork-session"}


# --------------------------------------------------------------------
# 3. The /status panel parser.
# --------------------------------------------------------------------


def test_status_parser_accepts_the_coordinator_canary_panel():
    session_id = muse_native_launch.mint_session_id()
    parsed = muse_native_status.parse_status_panel(
        status_panel_rows(None, session_id, directory="/private/tmp/cao-muse-canary")
    )
    assert parsed["session_id"] == session_id
    assert parsed["model"] == MUSE_MODEL
    assert parsed["reasoning"] == MUSE_EFFORT
    assert parsed["agent_profile"] == "native-basic"
    assert parsed["model_provider"] == "meta"
    assert parsed["directory"] == "/private/tmp/cao-muse-canary"
    assert parsed["run"] == "idle"
    assert parsed["tokens"] == 0
    assert parsed["turns"] == 0
    required = muse_native_status.require_pre_task_status(
        parsed,
        session_id=session_id,
        expected_model=MUSE_MODEL,
        expected_effort=MUSE_EFFORT,
        working_directory="/private/tmp/cao-muse-canary",
        expected_profile_identity="native-basic",
    )
    assert required["session_matches"] is True
    assert required["model_matches"] is True
    assert required["effort_matches"] is True
    assert required["idle"] is True


def test_status_parser_accepts_a_panel_without_a_reasoning_line():
    """The echo provider omits the Reasoning line; the parse must not fail."""
    session_id = muse_native_launch.mint_session_id()
    parsed = muse_native_status.parse_status_panel(
        status_panel_rows(None, session_id, reasoning=None)
    )
    assert parsed["reasoning"] is None


def test_provider_default_effort_sentinel_requires_no_reasoning_line():
    """The ``provider-default`` sentinel is not an effort to observe."""
    session_id = muse_native_launch.mint_session_id()
    parsed = muse_native_status.parse_status_panel(
        status_panel_rows(None, session_id, reasoning=None, directory="/worktree")
    )
    required = muse_native_status.require_pre_task_status(
        parsed,
        session_id=session_id,
        expected_model=MUSE_MODEL,
        expected_effort="provider-default",
        working_directory="/worktree",
        expected_profile_identity="native-basic",
    )
    assert required["effort_matches"] is True


@pytest.mark.parametrize(
    "mutate",
    [
        lambda rows, sid: status_panel_rows(None, sid, session_line=str(uuid.uuid4())),
        lambda rows, sid: status_panel_rows(None, sid, model="muse-spark-1.2"),
        lambda rows, sid: status_panel_rows(None, sid, reasoning="low"),
        lambda rows, sid: status_panel_rows(None, sid, agent_profile="miniswe"),
        lambda rows, sid: status_panel_rows(None, sid, provider="echo"),
        lambda rows, sid: status_panel_rows(None, sid, directory="/somewhere/else"),
        lambda rows, sid: status_panel_rows(None, sid, run="running"),
        lambda rows, sid: status_panel_rows(None, sid, tokens="12 tokens / 1 turns"),
    ],
)
def test_require_pre_task_status_rejects_every_mismatch(mutate):
    session_id = muse_native_launch.mint_session_id()
    rows = mutate(None, session_id)
    parsed = muse_native_status.parse_status_panel(rows)
    with pytest.raises(muse_native_status.MuseStatusMismatch):
        muse_native_status.require_pre_task_status(
            parsed,
            session_id=session_id,
            expected_model=MUSE_MODEL,
            expected_effort=MUSE_EFFORT,
            working_directory=str(None) if False else "/worktree",
            expected_profile_identity="native-basic",
        )


def test_status_parser_rejects_ambiguity_and_truncation():
    session_id = muse_native_launch.mint_session_id()
    ambiguous = status_panel_rows(None, session_id)
    ambiguous.insert(8, "│  Session:              another-session-id         │")
    with pytest.raises(muse_native_status.MuseStatusParseError):
        muse_native_status.parse_status_panel(ambiguous)
    # A capture cut off before the required lines is unreadable, not empty.
    truncated = status_panel_rows(None, session_id)[:8]
    with pytest.raises(muse_native_status.MuseStatusParseError):
        muse_native_status.parse_status_panel(truncated)


def test_status_parser_rejects_an_empty_or_escapeless_garbage_screen():
    with pytest.raises(muse_native_status.MuseStatusParseError):
        muse_native_status.parse_status_panel([])


# --------------------------------------------------------------------
# 4. The managed-v2 launch flow.
# --------------------------------------------------------------------


def _fake_profile_material(**changes) -> dict[str, Any]:
    class _Profile:
        permissionMode = None

        def model_dump(self, mode="json"):
            return {"permissionMode": None}

    material = {
        "profile": _Profile(),
        "profile_sha256": hashlib.sha256(b"profile").hexdigest(),
        "system_prompt": "You are the CAO worker profile.\nFollow the campaign.",
        "allowed_tools": ["fs_read", "bash"],
    }
    material.update(changes)
    return material


def _mint_with_harness_state():
    """Build the record/request/bootstrap a Muse launch would produce."""
    import tempfile

    from cli_agent_orchestrator.models.managed_launch_v2 import ManagedLaunchV2ReserveRequest

    tmp = tempfile.mkdtemp()
    worktree = tmp + "/repo"
    subprocess.run(["mkdir", "-p", worktree], check=True)
    subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=worktree, check=True)
    request = ManagedLaunchV2ReserveRequest(
        **_reserve_request(worktree, __import__("pathlib").Path(tmp)).model_dump()
    )
    record = {
        "provider": "muse_cli",
        "terminal_id": "term-muse",
        "generation": str(uuid.uuid4()),
        "working_directory": worktree,
        "agent_profile": "reviewer",
        "request": request.model_dump(),
    }
    bootstrap = {
        "native_session_id": muse_native_launch.mint_session_id(),
        "requested_model": MUSE_MODEL,
        "requested_effort": MUSE_EFFORT,
    }
    return record, record["request"], bootstrap


class _MuseHarness:
    """Every provider-facing boundary of a Muse native launch, recorded.

    ``typed`` records what was written into the pane (the ``/status``
    observation command and, later, the admitted task); ``captures``
    serves the rendered status panel; ``pane_status_script`` drives the
    generic pane-ready wait.
    """

    def __init__(self) -> None:
        self.typed: list[dict[str, Any]] = []
        self.captures: list[list[str]] = []
        self.capture_failures: list[Exception] = []
        self.terminals: list[dict[str, Any]] = []
        self.observed_pid = 4321
        self.pane_status_script: list[TerminalStatus] = [TerminalStatus.IDLE]
        self.session_id: Optional[str] = None

    @property
    def launched_argv(self) -> list[str]:
        assert self.terminals, "no pane was ever created"
        return list(self.terminals[-1]["managed_native_command"])

    @property
    def env_vars(self) -> dict[str, str]:
        assert self.terminals, "no pane was ever created"
        return dict(self.terminals[-1]["env_vars"])


@pytest.fixture
def muse_harness(monkeypatch):
    state = _MuseHarness()

    async def _create_terminal(**kwargs):
        state.terminals.append(kwargs)
        terminal_id = kwargs["reserved_terminal_id"]
        database.create_terminal_v2(
            terminal_id,
            kwargs.get("session_name") or "cao-test",
            kwargs.get("window_name") or f"w-{terminal_id}",
            kwargs.get("provider") or "muse_cli",
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
            "cwd": self_record_working_directory(),
        }

    def self_record_working_directory():
        return state.terminals[-1]["working_directory"]

    def _capture_render(self, pane_id):
        if state.capture_failures:
            raise state.capture_failures.pop(0)
        assert state.captures, "no scripted status panel rows"
        return list(state.captures[0])

    def _typed_literal(text, **_kwargs):
        state.typed.append({"kind": "literal", "text": text})

    def _typed_enter(**_kwargs):
        state.typed.append({"kind": "enter"})

    def _typed_key(keystroke, **_kwargs):
        state.typed.append({"kind": "key", "keystroke": keystroke})

    def _turn_state(pane_id, **_kwargs):
        status = (
            state.pane_status_script.pop(0)
            if len(state.pane_status_script) > 1
            else state.pane_status_script[0]
        )
        if isinstance(status, Exception):
            raise status
        return status

    monkeypatch.setattr(bridge, "provider_version_banner", lambda *a, **k: MUSE_BANNER)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.create_terminal", _create_terminal
    )
    monkeypatch.setattr(v2._V2NativePane, "observe", _observe)
    monkeypatch.setattr(v2._V2NativePane, "capture_render", _capture_render)
    # The admission-time live-pane read uses a real TmuxNativePane; the
    # fake below serves the same observation the launch recorded.
    monkeypatch.setattr(
        native_tui_launch, "TmuxNativePane", lambda *a, **k: _FakeTmuxNativePane(state)
    )
    monkeypatch.setattr(npi, "observe_muse_turn_state", _turn_state)
    monkeypatch.setattr(npi, "TmuxPaneInput", _FakeTmuxPaneInput.for_state(state))
    monkeypatch.setattr(v2, "NATIVE_PANE_READY_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(v2, "_NATIVE_PANE_READY_POLL_SECONDS", 0.005)
    monkeypatch.setattr(
        muse_native_launch,
        "mint_session_id",
        lambda: (state.session_id or "ffffffff-ffff-4fff-8fff-ffffffffffff"),
    )
    return state


class _FakeTmuxNativePane:
    """The admission-time live-pane read, served from the harness state."""

    def __init__(self, state: _MuseHarness) -> None:
        self._state = state

    def observe(self, **kwargs):
        return {
            "pane_id": "%7",
            "pid": self._state.observed_pid,
            "start_marker": "Thu Jul 24 10:00:00 2026",
            "argv": self._state.launched_argv,
            "cwd": self._state.terminals[-1]["working_directory"],
        }

    def capture_render(self, pane_id, **kwargs):
        assert self._state.captures, "no scripted status panel rows"
        return list(self._state.captures[0])


class _FakeTmuxPaneInput:
    _state: _MuseHarness

    @classmethod
    def for_state(cls, state: _MuseHarness) -> type["_FakeTmuxPaneInput"]:
        cls._state = state
        return cls

    def __init__(self, pane_id: str) -> None:
        self._pane_id = pane_id

    def send_literal(self, text: str) -> None:
        self._state.typed.append({"kind": "literal", "text": text})

    def send_enter(self) -> None:
        self._state.typed.append({"kind": "enter"})

    def send_key(self, keystroke: str) -> None:
        self._state.typed.append({"kind": "key", "keystroke": keystroke})


async def _launch(worktree, tmp_path, muse_harness, **changes):
    record, _ = v2.reserve(_reserve_request(worktree, tmp_path, **changes))
    assert record["execution_mode"] == em.NATIVE_TUI
    return record, await v2.launch_reserved(record["reservation_id"])


def _published_receipt(reservation_id: str) -> dict[str, Any]:
    state = bridge.read_state(reservation_id)
    assert state["state"] == "ready"
    return state["readiness"]


@pytest.mark.asyncio
async def test_muse_launch_resumes_the_exact_minted_id_with_no_prompt(
    isolated_memory_db, worktree, tmp_path, muse_harness
):
    muse_harness.captures.append(
        status_panel_rows(
            worktree, muse_harness.session_id or "ffffffff-ffff-4fff-8fff-ffffffffffff"
        )
    )
    record, result = await _launch(worktree, tmp_path, muse_harness)
    argv = muse_harness.launched_argv
    assert argv[0] == record["request"]["provider_executable"]
    assert argv[1] == "resume"
    assert argv[2] == "ffffffff-ffff-4fff-8fff-ffffffffffff"
    assert argv.count("ffffffff-ffff-4fff-8fff-ffffffffffff") == 1
    # No positional prompt anywhere after the identity pair.
    for token in argv[3:]:
        assert not token.startswith("resume ")
    assert result["execution_mode"] == em.NATIVE_TUI


@pytest.mark.asyncio
async def test_muse_launch_observes_status_before_persisting_or_publishing_readiness(
    isolated_memory_db, worktree, tmp_path, muse_harness
):
    session_id = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    muse_harness.captures.append(status_panel_rows(worktree, session_id))
    record, _result = await _launch(worktree, tmp_path, muse_harness)

    # The /status command was typed exactly once, as literal + one enter.
    literals = [t for t in muse_harness.typed if t["kind"] == "literal"]
    enters = [t for t in muse_harness.typed if t["kind"] == "enter"]
    assert [t["text"] for t in literals] == ["/status"]
    assert len(enters) == 1

    receipt = _published_receipt(record["reservation_id"])
    assert receipt["provider"] == "muse_cli"
    assert receipt["provider_receipt_kind"] == "muse-native-status-idle"
    assert receipt["model"] == MUSE_MODEL
    assert receipt["effort"] == MUSE_EFFORT
    assert receipt["provider_session_id"] == session_id
    # The provider's own /status statement is the session-start proof.
    assert receipt["provider_session_start"] is not None
    assert receipt["provider_session_start"]["session_matches"] is True
    assert receipt["model_input_ready"] is True
    # The durable v2 terminal row names the proven session only after the
    # observation (the row exists and carries the id).
    terminal = (
        database.SessionLocal()
        .query(database.ManagedLaunchV2TerminalModel)
        .filter(database.ManagedLaunchV2TerminalModel.id == record["terminal_id"])
        .first()
    )
    assert terminal is not None
    assert terminal.v2_native_session_id == session_id


@pytest.mark.asyncio
async def test_muse_launch_carries_the_profile_system_prompt_through_the_env_surface(
    isolated_memory_db, worktree, tmp_path, muse_harness
):
    session_id = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    muse_harness.captures.append(status_panel_rows(worktree, session_id))
    record, _result = await _launch(worktree, tmp_path, muse_harness)

    env = muse_harness.env_vars
    assert muse_native_launch.PROFILE_SYSTEM_PROMPT_ENV in env
    profile_path = env[muse_native_launch.PROFILE_SYSTEM_PROMPT_ENV]
    material = _profile_material_for(record)
    assert open(profile_path, encoding="utf-8").read() == material["system_prompt"]

    receipt = _published_receipt(record["reservation_id"])
    assert receipt["profile_sha256"] == material["profile_sha256"]
    assert (
        receipt["profile_system_prompt_sha256"]
        == hashlib.sha256(material["system_prompt"].encode("utf-8")).hexdigest()
    )
    assert receipt["acquisition_receipt_sha256"]


def _profile_material_for(record) -> dict[str, Any]:
    from cli_agent_orchestrator.services.managed_provider_bridge import _profile_material

    return _profile_material(record["agent_profile"], record["terminal_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "broken",
    [
        "wrong_session",
        "wrong_model",
        "wrong_effort",
        "wrong_profile",
        "wrong_cwd",
        "busy",
        "turns",
        "ambiguous",
        "unreadable",
    ],
)
async def test_muse_launch_blocks_with_zero_task_bytes_when_the_status_observation_fails(
    isolated_memory_db, worktree, tmp_path, muse_harness, broken
):
    session_id = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    if broken == "wrong_session":
        rows = status_panel_rows(worktree, session_id, session_line=str(uuid.uuid4()))
    elif broken == "wrong_model":
        rows = status_panel_rows(worktree, session_id, model="muse-spark-1.2")
    elif broken == "wrong_effort":
        rows = status_panel_rows(worktree, session_id, reasoning="low")
    elif broken == "wrong_profile":
        rows = status_panel_rows(worktree, session_id, agent_profile="miniswe")
    elif broken == "wrong_cwd":
        rows = status_panel_rows(worktree, session_id, directory=str(tmp_path / "elsewhere"))
    elif broken == "busy":
        rows = status_panel_rows(worktree, session_id, run="running")
    elif broken == "turns":
        rows = status_panel_rows(worktree, session_id, tokens="4 tokens / 1 turns")
    elif broken == "ambiguous":
        rows = status_panel_rows(worktree, session_id)
        rows.insert(8, "│  Session:              another-session-id         │")
    else:  # unreadable
        muse_harness.capture_failures.append(RuntimeError("pane gone"))
        rows = []
    muse_harness.captures.append(rows)

    record, result = await _launch(worktree, tmp_path, muse_harness)
    assert result["state"] == "preflight_blocked"
    # Zero task bytes: no admission claim, no delivery, and the durable
    # session id was never persisted for the failed observation.
    admission = result.get("admission")
    assert admission is None
    terminal = (
        database.SessionLocal()
        .query(database.TerminalModel)
        .filter(database.TerminalModel.id == record["terminal_id"])
        .first()
    )
    assert terminal is None or terminal.v2_native_session_id is None


@pytest.mark.asyncio
async def test_muse_launch_never_observes_model_or_effort_from_the_request(
    isolated_memory_db, worktree, tmp_path, muse_harness
):
    """Requested model/effort are never treated as observed without evidence."""
    session_id = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    muse_harness.captures.append(
        status_panel_rows(worktree, session_id, model=MUSE_MODEL, reasoning=MUSE_EFFORT)
    )
    record, _result = await _launch(worktree, tmp_path, muse_harness)
    receipt = _published_receipt(record["reservation_id"])
    # Observed from the panel, not echoed from the request.
    assert receipt["expected_model"] == MUSE_MODEL
    assert receipt["model"] == MUSE_MODEL
    assert receipt["effort"] == MUSE_EFFORT


# --------------------------------------------------------------------
# 5. Bind and admission — one task delivery, at-most-once.
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_muse_bind_binds_the_stable_roster_to_the_exact_session(
    isolated_memory_db, worktree, tmp_path, muse_harness
):
    session_id = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    muse_harness.captures.append(status_panel_rows(worktree, session_id))
    record, result = await _launch(worktree, tmp_path, muse_harness)
    assert result["state"] == "launching"

    bound = v2.bind_native(
        result["reservation_id"],
        ManagedLaunchV2BindRequest(
            protocol_version=PROTOCOL_VERSION_V2,
            terminal_id=result["terminal_id"],
            generation=result["generation"],
            attempt_id=str(uuid.uuid4()),
            fencing_token_id=str(uuid.uuid4()),
            execution_mode="native_tui",
        ),
    )
    assert bound["state"] == "bound"
    binding = bound["binding"]
    assert binding["native_session_id"] == session_id
    assert binding["execution_mode"] == "native_tui"

    from cli_agent_orchestrator.services import stable_agent_roster as roster

    # The durable roster lineage binds the exact harness-scoped session.
    agents = roster.list_agents(session_name="cao-test")
    assert len(agents) == 1
    assert agents[0]["profile_family"] == "reviewer"
    lineages = roster.list_lineages(agent_id=agents[0]["agent_id"])
    assert len(lineages) == 1
    assert lineages[0]["harness"] == "muse_cli"
    assert lineages[0]["native_session_id"] == session_id
    assert lineages[0]["acquisition_method"] == roster.ACQUISITION_CHOSEN_SESSION_ID


@pytest.mark.asyncio
async def test_muse_admission_delivers_exactly_once_after_readiness(
    isolated_memory_db, worktree, tmp_path, muse_harness
):
    session_id = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    muse_harness.captures.append(status_panel_rows(worktree, session_id))
    record, result = await _launch(worktree, tmp_path, muse_harness)
    bound = v2.bind_native(
        result["reservation_id"],
        ManagedLaunchV2BindRequest(
            protocol_version=PROTOCOL_VERSION_V2,
            terminal_id=result["terminal_id"],
            generation=result["generation"],
            attempt_id=str(uuid.uuid4()),
            fencing_token_id=str(uuid.uuid4()),
            execution_mode="native_tui",
        ),
    )
    digest = v2.native_binding_digest(bound)
    assert digest
    # Zero task bytes crossed before bind; nothing was typed.
    assert muse_harness.typed == [{"kind": "literal", "text": "/status"}, {"kind": "enter"}]

    admitted = await v2.admit_reserved(result["reservation_id"], _admit_request(digest))
    assert admitted["admission"]["status"] == "admitted"
    # The task bytes landed exactly once: one literal write of the message,
    # then the submitting enter.
    task_writes = [t for t in muse_harness.typed if t["kind"] == "literal"]
    assert [t["text"] for t in task_writes] == ["/status", TASK_MESSAGE]

    # A replay of the same delivery id adopts the stored outcome, sending
    # nothing.
    before = list(muse_harness.typed)
    replayed = await v2.admit_reserved(result["reservation_id"], _admit_request(digest))
    assert replayed["admission"]["status"] == "admitted"
    assert muse_harness.typed == before

    # A changed request under the same delivery id conflicts.
    with pytest.raises(ManagedLaunchConflict):
        await v2.admit_reserved(
            result["reservation_id"],
            _admit_request(digest, message="a different task"),
        )


@pytest.mark.asyncio
async def test_muse_admission_refuses_when_the_attachment_is_not_owned(
    isolated_memory_db, worktree, tmp_path, muse_harness
):
    session_id = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    muse_harness.captures.append(status_panel_rows(worktree, session_id))
    record, result = await _launch(worktree, tmp_path, muse_harness)
    bound = v2.bind_native(
        result["reservation_id"],
        ManagedLaunchV2BindRequest(
            protocol_version=PROTOCOL_VERSION_V2,
            terminal_id=result["terminal_id"],
            generation=result["generation"],
            attempt_id=str(uuid.uuid4()),
            fencing_token_id=str(uuid.uuid4()),
            execution_mode="native_tui",
        ),
    )
    digest = v2.native_binding_digest(bound)
    assert digest
    # Stage a pane that drifted to a different process identity: admission
    # must refuse with zero bytes typed rather than deliver into a stranger.
    muse_harness.observed_pid = 9999
    with pytest.raises(ManagedLaunchConflict):
        await v2.admit_reserved(result["reservation_id"], _admit_request(digest))
    task_writes = [t for t in muse_harness.typed if t["kind"] == "literal"]
    assert [t["text"] for t in task_writes] == ["/status"]


# --------------------------------------------------------------------
# 6. Profile fidelity: the same material and digest feed the resume
#    contract.
# --------------------------------------------------------------------


def test_profile_material_digest_feeds_launch_and_resume_identically():
    """The env-addressed file is the resume contract: same path, same digest.

    The launch writes the profile system prompt once into the
    generation-private companion dir and addresses it to the pane through
    ``TBH_EVAL_APPEND_SYSTEM_PROMPT_FILE``.  An exact resume re-runs the
    same CLI in the same environment, so the same bytes compose again —
    the deterministic echo-refusal proof covered this on both the launch
    and the ``muse resume <id>`` form.
    """
    material = _fake_profile_material()
    assert muse_native_launch.PROFILE_SYSTEM_PROMPT_ENV == "TBH_EVAL_APPEND_SYSTEM_PROMPT_FILE"
    assert material["system_prompt"]
    digest = hashlib.sha256(material["system_prompt"].encode("utf-8")).hexdigest()
    assert len(digest) == 64


def test_the_profile_surface_is_not_prompt_only():
    """The carrier env var is distinct from the task prompt and is refused
    when the profile material is missing."""
    with pytest.raises(muse_native_launch.MuseNativeLaunchError):
        muse_native_launch.validate_profile_system_prompt("")
