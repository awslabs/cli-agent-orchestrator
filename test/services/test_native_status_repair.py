"""Panel-attested native /status identity repair (cond-0377C review round).

Covers the reviewed contract end to end over fakes:

* A. legacy/generationless identity: legacy rows use generation ``None``
  plus the durable callback-target occurrence; teardown is serialized by
  the same canonical lifecycle claim set.
* B. branded pinned parsers: every provider requires exactly one
  brand/version header and its strict fields, returns the panel-attested
  build, and never echoes raw pane values.
* C. known-identity preflight before bytes (already-known, conflict,
  one-sided match/mismatch, attachment-unresolved).
* D. explicit operation-id idempotency (exact retry adopts evidence, a
  changed request is a typed conflict before pane I/O).
* E. redaction: a secret sentinel in malformed pane text is absent from
  service results and HTTP details.
* F. cancellation shields provider cleanup under the shared claims.
* G. detached-adoption CAS regression and sanitized full panels for all
  four providers.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import uuid
from typing import Any, Optional

import pytest

from cli_agent_orchestrator import constants
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.tmux import PaneControlIdentity, TmuxClient
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import execution_mode as em
from cli_agent_orchestrator.services import managed_launch_v2 as v2
from cli_agent_orchestrator.services import native_attachment
from cli_agent_orchestrator.services import native_pane_input as npi
from cli_agent_orchestrator.services import native_status_repair as nsr
from cli_agent_orchestrator.services import pane_input_arbiter as pia
from cli_agent_orchestrator.services import stable_agent_roster as roster

CLAUDE_VERSION = "2.1.226"
CODEX_VERSION = "0.147.0"
KIMI_VERSION = "0.34.0"
MUSE_VERSION = "0.1.0"

CLAUDE_BRAND_HEADER = "Settings  Status   Config   Usage   Stats"
CODEX_BRAND = ">_ OpenAI Codex (v0.147.0)"
KIMI_BRAND = ">_ Kimi Code (v0.34.0)"
MUSE_BRAND = ">_ Muse Code (0.1.0)"

#: The canary's exact session id (Claude 2.1.226 fixture), reused across
#: providers since all four render canonical UUIDs.
SESSION_ID = "4f5f46c7-b660-4f6f-a144-d2c6dceccf95"
KIMI_SESSION_ID = f"session_{SESSION_ID}"

TERMINAL_ID = "a1b2c3d4"
GENERATION = "00000000-0000-4000-8000-000000000001"
#: The durable physical occurrence for a legacy terminal.
CALLBACK_TARGET = "00000000-0000-4000-8000-0000000000aa"
PANE_ID = "%7"
WINDOW_ID = "@7"
TMUX_SESSION_ID = "$1"
SERVER_SOCKET = "/private/tmp/cao-native.sock"
PANE_PID = 4242
START_MARKER = "Thu Jul 24 10:00:00 2026"
SESSION_NAME = "cao-campaign"

#: A secret that must never reach a result or an HTTP detail.
SECRET = "super_secret_pane_value_zz9"


def _uuid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Panel fixtures (branded)
# ---------------------------------------------------------------------------


def claude_panel_rows(
    session_id: str = SESSION_ID,
    *,
    version: str = CLAUDE_VERSION,
    drop: tuple[str, ...] = (),
    duplicates: tuple[str, ...] = (),
    header: str = CLAUDE_BRAND_HEADER,
) -> list[str]:
    """The sanitized canary /status modal (with the literal ``[1m]`` styling
    fragments the plain capture retained)."""
    rows = [
        header,
        "",
        f"Version:          {version}",
        "Session name:     /rename to add a name",
        f"Session ID:       {session_id}",
        "Session kind:     interactive",
        "cwd:              /Users/x/repo",
        "Login method:     <redacted>",
        "Organization:     <redacted>",
        "Email:            <redacted>",
        "",
        "Model:            opus[1m] (claude-opus-5[1m])",
        "MCP servers:      <variable provider state>",
        "Setting sources:  User settings",
        "",
        "Esc to cancel",
    ]
    for label in duplicates:
        rows.append(f"Session ID:       {label}")
    if drop:
        rows = [row for row in rows if not any(row.lstrip().startswith(d) for d in drop)]
    return rows


def claude_composer_rows() -> list[str]:
    """The canary's post-Escape composer boundary capture."""
    return [
        "-------------------------------------------------------------------------------",
        "> ",
        "-------------------------------------------------------------------------------",
        "<quota/model/cwd status line>",
    ]


def codex_panel_rows(
    session_id: str = SESSION_ID,
    *,
    brand: str = CODEX_BRAND,
    extra: tuple[str, ...] = (),
) -> list[str]:
    rows = [
        brand,
        f"Session: {session_id}",
        "Model: gpt-5.4-codex",
        "cwd: /Users/x/repo",
    ]
    rows.extend(extra)
    return rows


def kimi_panel_rows(
    session_id: Optional[str] = KIMI_SESSION_ID,
    *,
    brand: str = KIMI_BRAND,
    extra: tuple[str, ...] = (),
    drop: tuple[str, ...] = (),
) -> list[str]:
    """The Kimi status panel, box-styled.  ``session_id=None`` renders the
    exact ``Session none`` fresh/no-turn missing-ID panel."""
    rows = [
        "╭────────────────────────────────────────────╮",
        f"│ {brand}",
        "│ Model: kimi-k2",
    ]
    if session_id is not None:
        rows.append(f"│ Session {session_id}")
    else:
        rows.append("│ Session none")
    rows.append("╰────────────────────────────────────────────╯")
    rows.extend(extra)
    if drop:
        rows = [row for row in rows if not any(token in row for token in drop)]
    return rows


def muse_panel_rows(
    session_id: str = SESSION_ID,
    *,
    brand: str = MUSE_BRAND,
    tokens: str = "120 tokens / 3 turns",
    run: str = "idle",
) -> list[str]:
    return [
        brand,
        "╭────────────────────────────────────────────╮",
        "│ Session: " + session_id,
        "│ Model: muse-spark-1.2-contributor (reasoning high)",
        "│ Agent profile: native-basic",
        "│ Model provider: meta",
        "│ Directory: /Users/x/repo",
        f"│ Run: {run}",
        f"│ Token usage: {tokens}",
        "╰────────────────────────────────────────────╯",
        "⟩ ",
    ]


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _RepairHarness:
    def __init__(self) -> None:
        self.typed: list[dict[str, Any]] = []
        self.screens: list[list[str]] = []
        self.styled_screens: list[list[str]] = []
        self.capture_errors: list[Exception] = []
        self.turn_states: list[TerminalStatus] = [TerminalStatus.IDLE]
        self.pane_identity: Optional[PaneControlIdentity] = PaneControlIdentity(
            pane_id=PANE_ID,
            window_id=WINDOW_ID,
            session_id=TMUX_SESSION_ID,
            pane_pid=PANE_PID,
            session_name=SESSION_NAME,
            window_name=f"w-{TERMINAL_ID}",
            bracketed_paste_proven=False,
            dead=False,
            server_socket_path=SERVER_SOCKET,
        )
        self.pane_identity_error: Optional[Exception] = None
        self.server_identity: Optional[str] = SERVER_SOCKET
        self.live_start_marker: Optional[str] = START_MARKER
        self.escapes: int = 0
        self.lease_held_at_escape: bool = False
        self.block: Optional[threading.Event] = None
        self.composer_proof_rows: Optional[list[str]] = None
        self.calls: list[str] = []
        # cond-0427: the fake models a composer, because the submission
        # barrier's whole job is to read one.  ``_composer`` holds whatever
        # was typed and is given up on Enter, so a repair against a
        # barrier-pinned provider observes a real submit boundary instead of
        # a return code.  Set ``composer_keeps_text`` to model a pane that
        # takes the bytes and never submits them.
        self._composer: str = ""
        self.composer_keeps_text: bool = False
        self.composer_drops_literal: bool = False

    #: Rows appended below every captured screen to render the composer.
    #: Five covers the widest ``composer_tail_rows`` in the barrier table,
    #: so the text lands inside the observed region for every provider.
    _COMPOSER_ROWS = 5

    def _composer_rows(self) -> list[str]:
        return ["" for _ in range(self._COMPOSER_ROWS - 1)] + [f"> {self._composer}"]

    def turn_state(self, pane_id: str, **_kwargs: Any) -> TerminalStatus:
        self.calls.append("turn-state")
        status = self.turn_states[-1]
        if len(self.turn_states) > 1:
            self.turn_states.pop(0)
        if isinstance(status, Exception):
            raise status
        return status

    def capture_screen(self, pane_id: str, **_kwargs: Any) -> list[str]:
        self.calls.append("capture")
        if self.block is not None:
            self.block.wait(timeout=60)
        if self.capture_errors:
            raise self.capture_errors.pop(0)
        assert self.screens, "no scripted panel rows"
        return list(self.screens[-1]) + self._composer_rows()

    def capture_screen_styled(self, pane_id: str, **_kwargs: Any) -> list[str]:
        self.calls.append("capture-styled")
        if self.composer_proof_rows is not None:
            return list(self.composer_proof_rows)
        assert self.styled_screens, "no scripted post-Escape rows"
        return list(self.styled_screens[-1])

    def pane_control_identity(self, *args: Any, **kwargs: Any) -> Optional[PaneControlIdentity]:
        self.calls.append("pane-identity")
        if self.pane_identity_error is not None:
            raise self.pane_identity_error
        return self.pane_identity

    def pane_server_identity(self, pane_id: str, *args: Any, **kwargs: Any) -> Optional[str]:
        self.calls.append("server-identity")
        return self.server_identity

    def start_marker(self, pid: int) -> Optional[str]:
        self.calls.append("start-marker")
        return self.live_start_marker

    def typed_literal(self, text: str) -> None:
        self.typed.append({"kind": "literal", "text": text})
        if not self.composer_drops_literal:
            self._composer = text

    def typed_enter(self) -> None:
        self.typed.append({"kind": "enter"})
        if not self.composer_keeps_text:
            self._composer = ""

    def typed_key(self, keystroke: str) -> None:
        if keystroke == "Escape":
            self.escapes += 1
            self.lease_held_at_escape = pia.is_pane_leased(PANE_ID)
        self.typed.append({"kind": "key", "keystroke": keystroke})


class _FakeTmuxPaneInput:
    _state: _RepairHarness

    @classmethod
    def for_state(cls, state: _RepairHarness) -> type["_FakeTmuxPaneInput"]:
        cls._state = state
        return cls

    def __init__(self, pane_id: str) -> None:
        self._pane_id = pane_id

    def send_literal(self, text: str) -> None:
        self._state.typed_literal(text)

    def send_enter(self) -> None:
        self._state.typed_enter()

    def send_key(self, keystroke: str) -> None:
        self._state.typed_key(keystroke)


@pytest.fixture(autouse=True)
def _sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "COMPANION_DIR", tmp_path / "companion")
    monkeypatch.setattr(constants, "CAO_HOME_DIR", tmp_path / "home")


@pytest.fixture
def harness(monkeypatch):
    state = _RepairHarness()
    monkeypatch.setattr(npi, "TmuxPaneInput", _FakeTmuxPaneInput.for_state(state))
    monkeypatch.setattr(npi, "capture_pane_screen", state.capture_screen)
    monkeypatch.setattr(npi, "capture_pane_screen_styled", state.capture_screen_styled)
    for observer in (
        "observe_codex_turn_state",
        "observe_kimi_turn_state",
        "observe_claude_turn_state",
        "observe_muse_turn_state",
    ):
        monkeypatch.setattr(npi, observer, state.turn_state)
    monkeypatch.setattr(TmuxClient, "pane_control_identity", state.pane_control_identity)
    monkeypatch.setattr(TmuxClient, "observe_pane_server_identity", state.pane_server_identity)
    monkeypatch.setattr(nsr, "_live_start_marker", state.start_marker)
    monkeypatch.setattr(v2, "NATIVE_PANE_READY_TIMEOUT_SECONDS", 0.4)
    monkeypatch.setattr(v2, "_NATIVE_PANE_READY_POLL_SECONDS", 0.005)
    return state


PINNED_VERSION = {
    "claude_code": CLAUDE_VERSION,
    "codex": CODEX_VERSION,
    "kimi_cli": KIMI_VERSION,
    "muse_cli": MUSE_VERSION,
}

#: Sentinel: seed the default valid managed-v2 binding in _seed_terminal.
_BINDING_DEFAULT = object()


def _seed_v2_reservation(
    provider: str,
    *,
    terminal_id: str = TERMINAL_ID,
    generation: str = GENERATION,
    binding: Optional[dict] = None,
    state: str = "bound",
) -> None:
    """Create or update the managed-v2 reservation for a v2 terminal.
    ``binding`` of None means the reservation exists with no binding_json."""
    import json as _json

    with database.SessionLocal() as db:
        existing = (
            db.query(database.ManagedLaunchV2ReservationModel)
            .filter_by(terminal_id=terminal_id)
            .first()
        )
        if existing is None:
            db.add(
                database.ManagedLaunchV2ReservationModel(
                    reservation_id=_uuid(),
                    terminal_id=terminal_id,
                    generation=generation,
                    protocol_vintage="v2",
                    session_name=SESSION_NAME,
                    provider=provider,
                    agent_profile="developer",
                    caller_id="deadbeef",
                    working_directory="/Users/x/repo",
                    obligation_generation=generation,
                    run_id="run-1",
                    launch_nonce_digest="a" * 64,
                    state=state,
                    request_json=_json.dumps({"expected_model": "m", "expected_effort": "high"}),
                    binding_json=_json.dumps(binding) if binding is not None else None,
                    execution_mode="native_tui",
                    created_at="now",
                    updated_at="now",
                )
            )
        else:
            existing.generation = generation
            existing.state = state
            existing.binding_json = _json.dumps(binding) if binding is not None else None
            existing.execution_mode = "native_tui"
        db.commit()


def _default_binding(provider: str, session_id: str, version: Optional[str] = None) -> dict:
    return {
        "schema": "cao-managed-v2-native-binding-v1",
        "execution_mode": "native_tui",
        "native_session_id": session_id,
        "provider_version": version or PINNED_VERSION[provider],
    }


def _seed_terminal(
    provider: str,
    *,
    terminal_id: str = TERMINAL_ID,
    generation: str = GENERATION,
    lifecycle: str = "live",
    native_session_id: Optional[str] = None,
    pane_id: str = PANE_ID,
    window_id: str = WINDOW_ID,
    pane_pid: int = PANE_PID,
    server_socket: str = SERVER_SOCKET,
    binding_session_id: str = SESSION_ID,
    binding_version: Optional[str] = None,
    binding: Optional[dict] = _BINDING_DEFAULT,
) -> None:
    database.create_terminal_v2(
        terminal_id,
        SESSION_NAME,
        f"w-{terminal_id}",
        provider,
        generation=generation,
        pane_id=pane_id,
        window_id=window_id,
        server_socket_path=server_socket,
        session_id=TMUX_SESSION_ID,
        pane_pid=pane_pid,
    )
    with database.SessionLocal() as db:
        row = (
            db.query(database.ManagedLaunchV2TerminalModel)
            .filter(database.ManagedLaunchV2TerminalModel.id == terminal_id)
            .first()
        )
        row.v2_lifecycle_state = lifecycle
        row.v2_native_session_id = native_session_id
        db.commit()
    if provider in PINNED_VERSION:
        if binding is _BINDING_DEFAULT:
            binding = _default_binding(provider, binding_session_id, binding_version)
        _seed_v2_reservation(
            provider, terminal_id=terminal_id, generation=generation, binding=binding
        )


def _seed_legacy(
    provider: str,
    *,
    terminal_id: str = TERMINAL_ID,
    callback_target: str = CALLBACK_TARGET,
    lifecycle: str = "live",
    native_session_id: Optional[str] = None,
) -> None:
    """A real legacy TerminalModel row: generation None, a durable
    callback-target occurrence, and the exact pane tuple."""
    with database.SessionLocal() as db:
        db.add(
            database.TerminalModel(
                id=terminal_id,
                tmux_session=SESSION_NAME,
                tmux_window=f"w-{terminal_id}",
                provider=provider,
                generation=None,
                callback_target_generation=callback_target,
                pane_id=PANE_ID,
                window_id=WINDOW_ID,
                server_socket_path=SERVER_SOCKET,
                session_id=TMUX_SESSION_ID,
                pane_pid=PANE_PID,
                native_session_id=native_session_id,
                lifecycle_state=lifecycle,
            )
        )
        db.commit()


def _seed_roster(
    provider: str,
    *,
    terminal_id: str = TERMINAL_ID,
    generation: Optional[str] = GENERATION,
    native_session_id: Optional[str] = None,
    harness: Optional[str] = None,
    start_marker: str = START_MARKER,
    pane_pid: int = PANE_PID,
    pane_id: str = PANE_ID,
) -> dict[str, Any]:
    return roster.bind_generation(
        roster.BindingContract(
            agent_id=_uuid(),
            session_name=SESSION_NAME,
            role=roster.ROLE_WORKER,
            profile_family="developer",
            harness=harness or provider,
            native_session_id=native_session_id,
            terminal_id=terminal_id,
            generation=generation,
            pane_id=pane_id,
            pane_pid=pane_pid,
            process_identity={"pid": pane_pid, "start_marker": start_marker},
            execution_mode=em.NATIVE_TUI,
        )
    )


def _seed_all(provider: str, *, binding_session_id: str = SESSION_ID) -> dict[str, Any]:
    _seed_terminal(provider, binding_session_id=binding_session_id)
    return _seed_roster(provider)


def _seed_legacy_all(provider: str) -> dict[str, Any]:
    _seed_legacy(provider)
    return _seed_roster(provider, generation=None)


def _call(**changes: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "terminal_id": TERMINAL_ID,
        "generation": GENERATION,
        "provider_version": CLAUDE_VERSION,
        "operation_id": _uuid(),
    }
    payload.update(changes)
    return nsr.repair_terminal_native_identity(**payload)


def _typed_bytes(state: _RepairHarness) -> list[tuple[str, str]]:
    return [
        (entry["kind"], entry.get("text") or entry.get("keystroke") or "") for entry in state.typed
    ]


def _terminal_row(terminal_id: str = TERMINAL_ID) -> Any:
    with database.SessionLocal() as db:
        return (
            db.query(database.ManagedLaunchV2TerminalModel)
            .filter(database.ManagedLaunchV2TerminalModel.id == terminal_id)
            .first()
        )


def _legacy_row(terminal_id: str = TERMINAL_ID) -> Any:
    with database.SessionLocal() as db:
        return (
            db.query(database.TerminalModel)
            .filter(database.TerminalModel.id == terminal_id)
            .first()
        )


def _current_lineage(
    terminal_id: str = TERMINAL_ID, generation: Optional[str] = GENERATION
) -> dict[str, Any]:
    incarnation = roster.get_incarnation_by_terminal(terminal_id, generation=generation)
    agent = roster.get_agent(incarnation["agent_id"])
    return agent["current_lineage"]


def _evidence_rows() -> list[Any]:
    with database.SessionLocal() as db:
        return db.query(database.NativeStatusRepairEvidenceModel).all()


def _declare_attachment(
    provider: str, session_id: str, *, terminal_id: str, generation: str
) -> None:
    native_attachment.declare(
        provider=provider,
        native_session_id=session_id,
        terminal_id=terminal_id,
        generation=generation,
        execution_mode=em.NATIVE_TUI,
        intent=native_attachment.acquire_intent(
            acquisition_method=native_attachment.ACQUISITION_CHOSEN_SESSION_ID,
            acquisition_receipt={"schema": "test-intent"},
            admits_only_new_instructions=True,
            replays_task_bytes=False,
        ),
    )


def _seed_exact_attached_attachment(
    provider: str,
    session_id: str,
    *,
    terminal_id: str,
    generation: str,
    pane_id: str = PANE_ID,
    process_identity: Optional[dict[str, Any]] = None,
) -> None:
    """Declare + start + attach a claim whose owner is exactly the repair's
    current facts (the default pane/process tuple)."""
    identity = process_identity or {"pid": PANE_PID, "start_marker": START_MARKER}
    native_attachment.declare(
        provider=provider,
        native_session_id=session_id,
        terminal_id=terminal_id,
        generation=generation,
        execution_mode=em.NATIVE_TUI,
        intent=native_attachment.acquire_intent(
            acquisition_method=native_attachment.ACQUISITION_CHOSEN_SESSION_ID,
            acquisition_receipt={"schema": "test-intent"},
            admits_only_new_instructions=True,
            replays_task_bytes=False,
        ),
    )
    native_attachment.mark_starting(
        provider=provider,
        native_session_id=session_id,
        terminal_id=terminal_id,
        generation=generation,
        execution_mode=em.NATIVE_TUI,
    )
    native_attachment.mark_attached(
        provider=provider,
        native_session_id=session_id,
        terminal_id=terminal_id,
        generation=generation,
        execution_mode=em.NATIVE_TUI,
        process_identity=identity,
        pane_id=pane_id,
    )


# ---------------------------------------------------------------------------
# B: parser unit tests
# ---------------------------------------------------------------------------


class TestClaudeParser:
    def test_accepts_the_canary_panel_with_styling_fragments(self):
        parsed = nsr.parse_claude_status(claude_panel_rows(), pinned_version=CLAUDE_VERSION)
        assert parsed["session_id"] == SESSION_ID
        assert parsed["parser_key"] == "claude-modal-v1"
        assert parsed["provider_version"] == CLAUDE_VERSION

    def test_refuses_a_drifted_build_version(self):
        with pytest.raises(nsr.PanelParseError):
            nsr.parse_claude_status(
                claude_panel_rows(version="2.1.225"), pinned_version=CLAUDE_VERSION
            )

    def test_refuses_a_missing_version_row(self):
        with pytest.raises(nsr.PanelParseError):
            nsr.parse_claude_status(
                claude_panel_rows(drop=("Version:",)), pinned_version=CLAUDE_VERSION
            )

    def test_refuses_duplicate_session_rows_and_stale_prior_panels(self):
        with pytest.raises(nsr.PanelParseError):
            nsr.parse_claude_status(
                claude_panel_rows(duplicates=("11111111-2222-4333-8444-555555555555",)),
                pinned_version=CLAUDE_VERSION,
            )

    def test_refuses_a_missing_header(self):
        with pytest.raises(nsr.PanelParseError):
            nsr.parse_claude_status(
                claude_panel_rows(header="something else entirely"),
                pinned_version=CLAUDE_VERSION,
            )

    def test_refuses_a_malformed_session_id_without_echoing_it(self):
        with pytest.raises(nsr.PanelParseError) as exc:
            nsr.parse_claude_status(
                claude_panel_rows(session_id=SECRET), pinned_version=CLAUDE_VERSION
            )
        assert SECRET not in str(exc.value)

    def test_refuses_an_uppercase_session_id(self):
        with pytest.raises(nsr.PanelParseError):
            nsr.parse_claude_status(
                claude_panel_rows(session_id=SESSION_ID.upper()), pinned_version=CLAUDE_VERSION
            )

    def test_refuses_a_codex_panel(self):
        with pytest.raises(nsr.PanelParseError):
            nsr.parse_claude_status(codex_panel_rows(), pinned_version=CLAUDE_VERSION)

    def test_refuses_a_missing_session_row(self):
        with pytest.raises(nsr.PanelParseError):
            nsr.parse_claude_status(
                claude_panel_rows(drop=("Session ID:",)), pinned_version=CLAUDE_VERSION
            )


class TestClaudeComposerProof:
    def test_rejects_a_modal_remnant_despite_prompt_and_divider(self):
        # Prompt + divider markers present but a Session ID: modal remnant
        # still on screen: not a restored composer.
        rows = ["---", "> ", "Session ID: 00000000-0000-4000-8000-000000000001", "---"]
        assert nsr._claude_composer_restored(rows) is False

    def test_accepts_the_clean_composer_boundary(self):
        assert nsr._claude_composer_restored(claude_composer_rows()) is True


class TestCodexParser:
    def test_accepts_the_pinned_branded_panel(self):
        parsed = nsr.parse_codex_status(codex_panel_rows())
        assert parsed["session_id"] == SESSION_ID
        assert parsed["provider_version"] == CODEX_VERSION

    def test_refuses_a_bare_session_row_without_a_brand_header(self):
        # The coordinator red repro: "Session: <uuid>" alone is not a panel.
        with pytest.raises(nsr.PanelParseError):
            nsr.parse_codex_status([f"Session: {SESSION_ID}"])

    def test_refuses_a_missing_brand_header(self):
        with pytest.raises(nsr.PanelParseError):
            nsr.parse_codex_status(codex_panel_rows(brand="something else"))

    def test_refuses_duplicate_brand_headers(self):
        with pytest.raises(nsr.PanelParseError):
            nsr.parse_codex_status(codex_panel_rows(extra=(CODEX_BRAND,)))

    def test_refuses_a_mismatched_version_header(self):
        with pytest.raises(nsr.PanelParseError):
            nsr.parse_codex_status(codex_panel_rows(brand=">_ OpenAI Codex (v0.146.0)"))

    def test_refuses_duplicate_sessions(self):
        with pytest.raises(nsr.PanelParseError):
            nsr.parse_codex_status(
                codex_panel_rows(extra=("Session: 11111111-2222-4333-8444-555555555555",))
            )

    def test_refuses_a_malformed_session_value_without_echoing_it(self):
        with pytest.raises(nsr.PanelParseError) as exc:
            nsr.parse_codex_status(codex_panel_rows(session_id=SECRET))
        assert SECRET not in str(exc.value)

    def test_refuses_a_claude_modal_capture(self):
        with pytest.raises(nsr.PanelParseError):
            nsr.parse_codex_status(claude_panel_rows())


class TestKimiParser:
    def test_accepts_a_live_session_row(self):
        parsed = nsr.parse_kimi_status(kimi_panel_rows())
        assert parsed["session_id"] == KIMI_SESSION_ID
        assert parsed["provider_version"] == KIMI_VERSION

    def test_exact_session_none_is_a_typed_still_missing(self):
        parsed = nsr.parse_kimi_status(kimi_panel_rows(session_id=None))
        assert parsed["identity_still_missing"] is True
        assert "session_id" not in parsed

    def test_refuses_a_bare_session_none_without_a_brand_header(self):
        # The coordinator red repro: "Session none" alone is not a panel.
        with pytest.raises(nsr.PanelParseError):
            nsr.parse_kimi_status(["Session none"])

    def test_refuses_session_dash(self):
        with pytest.raises(nsr.PanelParseError):
            nsr.parse_kimi_status(kimi_panel_rows(session_id=None, drop=("none",)) + ["Session -"])

    def test_refuses_session_nonsense(self):
        with pytest.raises(nsr.PanelParseError):
            nsr.parse_kimi_status(
                kimi_panel_rows(session_id=None, drop=("none",)) + ["Session nonsense"]
            )

    def test_refuses_duplicate_session_rows(self):
        with pytest.raises(nsr.PanelParseError):
            nsr.parse_kimi_status(kimi_panel_rows(extra=(f"Session session_{_uuid()}",)))

    def test_refuses_duplicate_session_none_rows(self):
        with pytest.raises(nsr.PanelParseError):
            nsr.parse_kimi_status(kimi_panel_rows(session_id=None, extra=("Session none",)))

    def test_refuses_a_malformed_session_id_without_echoing_it(self):
        with pytest.raises(nsr.PanelParseError) as exc:
            nsr.parse_kimi_status(kimi_panel_rows(session_id="session_" + SECRET))
        assert SECRET not in str(exc.value)

    def test_refuses_a_claude_modal_capture(self):
        with pytest.raises(nsr.PanelParseError):
            nsr.parse_kimi_status(claude_panel_rows())

    def test_refuses_garbage(self):
        with pytest.raises(nsr.PanelParseError):
            nsr.parse_kimi_status(["nothing here"])


class TestMuseParser:
    def test_accepts_a_post_work_panel_with_nonzero_turns(self):
        # The repair must NOT reuse the launch's pre-task zero-turn gate.
        parsed = nsr.parse_muse_status(muse_panel_rows(tokens="120 tokens / 3 turns"))
        assert parsed["session_id"] == SESSION_ID
        assert parsed["provider_version"] == MUSE_VERSION

    def test_refuses_a_missing_brand_header(self):
        with pytest.raises(nsr.PanelParseError):
            nsr.parse_muse_status(muse_panel_rows()[1:])

    def test_refuses_a_missing_session_row(self):
        with pytest.raises(nsr.PanelParseError):
            nsr.parse_muse_status(muse_panel_rows()[:3])

    def test_refuses_a_malformed_session_id_without_echoing_it(self):
        with pytest.raises(nsr.PanelParseError) as exc:
            nsr.parse_muse_status(muse_panel_rows(session_id=SECRET))
        assert SECRET not in str(exc.value)


class TestNormalization:
    def test_ansi_style_and_box_drawing_stripped_deterministically(self):
        styled = [
            "\x1b[1m│ >_ Kimi Code (v0.34.0) \x1b[0m",
            "  \x1b[2m│ Session session_x\x1b[0m  ",
        ]
        plain = nsr.normalize_capture_rows(styled)
        assert plain[0] == "Kimi Code (v0.34.0)"
        assert plain[1] == "Session session_x"

    def test_evidence_digest_is_bounded_and_deterministic(self):
        rows = claude_panel_rows()
        first = nsr.evidence_digest(rows)
        assert re.fullmatch(r"[0-9a-f]{64}", first)
        assert nsr.evidence_digest(list(rows)) == first
        assert nsr.evidence_digest(["\x1b[1m" + row for row in rows]) == first
        huge = ["x" * 10000] * 3000
        assert re.fullmatch(r"[0-9a-f]{64}", nsr.evidence_digest(huge))


# ---------------------------------------------------------------------------
# Happy paths per provider
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "provider, version, panel, typed, parser_key, escape, expected_id",
    [
        pytest.param(
            "claude_code",
            CLAUDE_VERSION,
            claude_panel_rows(),
            [("literal", "/status"), ("enter", ""), ("key", "Escape")],
            "claude-modal-v1",
            True,
            SESSION_ID,
            id="claude",
        ),
        pytest.param(
            "codex",
            CODEX_VERSION,
            codex_panel_rows(),
            [("literal", "/status"), ("enter", "")],
            "codex-status-v1",
            False,
            SESSION_ID,
            id="codex",
        ),
        pytest.param(
            "kimi_cli",
            KIMI_VERSION,
            kimi_panel_rows(),
            [("literal", "/status"), ("enter", "")],
            "kimi-status-v1",
            False,
            KIMI_SESSION_ID,
            id="kimi",
        ),
        pytest.param(
            "muse_cli",
            MUSE_VERSION,
            muse_panel_rows(tokens="120 tokens / 3 turns"),
            [("literal", "/status"), ("enter", "")],
            "muse-panel-v1",
            False,
            SESSION_ID,
            id="muse",
        ),
    ],
)
def test_repair_happy_path_per_provider(
    isolated_memory_db, harness, provider, version, panel, typed, parser_key, escape, expected_id
):
    harness.screens.append(panel)
    if provider == "claude_code":
        harness.styled_screens.append(claude_composer_rows())
    _seed_all(provider, binding_session_id=expected_id)

    outcome = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        provider_version=version,
        operation_id=_uuid(),
    )

    assert outcome["status"] == "repaired"
    assert outcome["reason"] is None
    assert outcome["native_session_id"] == expected_id
    assert outcome["parser_key"] == parser_key
    assert outcome["provider"] == provider
    assert outcome["provider_version"] == version
    assert outcome["task_bytes_submitted"] is False
    assert re.fullmatch(r"[0-9a-f]{64}", outcome["evidence_sha256"])
    assert _typed_bytes(harness) == typed

    assert _terminal_row().v2_native_session_id == expected_id
    lineage = _current_lineage()
    assert lineage["native_session_id"] == expected_id
    assert lineage["lineage_origin"] == roster.LINEAGE_ORIGIN_REPAIR
    assert lineage["acquisition_method"] == native_attachment.ACQUISITION_STATUS_DISCOVERED
    assert lineage["continuity_note"] and "status repair" in lineage["continuity_note"]

    attachment = native_attachment.get(provider, expected_id)
    assert attachment is not None
    assert attachment["state"] == native_attachment.ATTACHED
    owner = attachment["owner"]
    assert owner["terminal_id"] == TERMINAL_ID
    assert owner["generation"] == GENERATION
    assert owner["pane_id"] == PANE_ID
    assert owner["process_identity"] == {"pid": PANE_PID, "start_marker": START_MARKER}
    receipt = attachment["adoption_receipt"]
    assert receipt["schema"] == native_attachment.STATUS_REPAIR_ADOPTION_SCHEMA
    assert receipt["evidence_sha256"] == outcome["evidence_sha256"]
    assert receipt["parser_key"] == parser_key
    assert receipt["provider_version"] == version
    assert receipt["pane_id"] == PANE_ID

    evidence = _evidence_rows()
    assert len(evidence) == 1
    assert evidence[0].native_session_id == expected_id
    assert evidence[0].provider_version == version
    assert evidence[0].generation == GENERATION

    if escape:
        assert harness.escapes == 1
        assert harness.lease_held_at_escape is True
        assert outcome["composer_restored"] is True
    else:
        assert harness.escapes == 0


def test_kimi_no_id_is_typed_still_missing_with_zero_mutation(isolated_memory_db, harness):
    # A fresh, never-sessioned Kimi pane has no binding to constrain it: use
    # a legacy Kimi row (generation None) so the only identity surface is
    # the panel's exact 'Session none' verdict.
    harness.screens.append(kimi_panel_rows(session_id=None))
    _seed_legacy("kimi_cli")
    _seed_roster("kimi_cli", generation=None)

    outcome = _call(
        provider_version=KIMI_VERSION,
        generation=None,
        physical_occurrence=CALLBACK_TARGET,
    )
    assert outcome["status"] == "identity-still-missing"
    assert outcome["native_session_id"] is None
    assert outcome["evidence_sha256"] is None
    assert outcome["provider_version"] == KIMI_VERSION
    assert _typed_bytes(harness) == [("literal", "/status"), ("enter", "")]
    assert _legacy_row().native_session_id is None
    assert _current_lineage(generation=None)["native_session_id"] is None
    assert native_attachment.get("kimi_cli", "anything") is None
    assert _evidence_rows() == []


# ---------------------------------------------------------------------------
# A: legacy/generationless identity
# ---------------------------------------------------------------------------


def test_legacy_happy_path_binds_the_callback_target_occurrence(isolated_memory_db, harness):
    _seed_legacy_all("claude_code")
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())

    outcome = _call(generation=None, physical_occurrence=CALLBACK_TARGET)
    assert outcome["status"] == "repaired"
    assert outcome["generation"] == CALLBACK_TARGET
    assert outcome["physical_occurrence"] == CALLBACK_TARGET
    assert outcome["model_generation"] is None
    assert outcome["native_session_id"] == SESSION_ID

    row = _legacy_row()
    assert row.native_session_id == SESSION_ID
    lineage = _current_lineage(generation=None)
    assert lineage["native_session_id"] == SESSION_ID
    attachment = native_attachment.get("claude_code", SESSION_ID)
    assert attachment["owner"]["generation"] == CALLBACK_TARGET
    assert _evidence_rows()[0].generation == CALLBACK_TARGET


def test_legacy_row_refuses_a_supplied_expected_generation(isolated_memory_db, harness):
    _seed_legacy_all("claude_code")
    harness.screens.append(claude_panel_rows())

    outcome = _call(generation=GENERATION)
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "generation-mismatch"
    assert harness.typed == []
    assert _legacy_row().native_session_id is None


def test_managed_row_requires_the_exact_model_generation(isolated_memory_db, harness):
    _seed_all("claude_code")
    harness.screens.append(claude_panel_rows())
    outcome = _call(generation=None)
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "generation-required"
    assert harness.typed == []


def test_legacy_row_missing_callback_target_self_heals_or_refuses(isolated_memory_db, harness):
    # A terminals row with no callback target but a model generation
    # self-heals to a pane-bound occurrence through get_terminal_metadata
    # and repairs as a managed row under its exact generation.
    with database.SessionLocal() as db:
        db.add(
            database.TerminalModel(
                id=TERMINAL_ID,
                tmux_session=SESSION_NAME,
                tmux_window=f"w-{TERMINAL_ID}",
                provider="claude_code",
                generation=GENERATION,
                callback_target_generation=None,
                pane_id=PANE_ID,
                window_id=WINDOW_ID,
                server_socket_path=SERVER_SOCKET,
                session_id=TMUX_SESSION_ID,
                pane_pid=PANE_PID,
                lifecycle_state="live",
            )
        )
        db.commit()
    _seed_roster("claude_code", generation=GENERATION)
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())

    outcome = _call(generation=GENERATION)
    assert outcome["status"] == "repaired"
    assert _legacy_row().callback_target_generation == GENERATION

    # A true legacy row (generation None) with no callback target cannot
    # heal to a pane-bound occurrence: the seam would mint a random uuid,
    # which is refused as a non-mutating typed refusal.
    with database.SessionLocal() as db:
        row = (
            db.query(database.TerminalModel)
            .filter(database.TerminalModel.id == TERMINAL_ID)
            .first()
        )
        row.generation = None
        row.callback_target_generation = None
        db.commit()
    harness.screens.append(claude_panel_rows())
    harness.typed.clear()
    outcome = _call(generation=None)
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "callback-target-missing"
    assert harness.typed == []


def test_legacy_teardown_is_serialized_by_the_shared_lifecycle_claims(
    isolated_memory_db, harness, monkeypatch
):
    """Stop/delete cannot retire/release concurrently with a repair: both take
    the same canonical lifecycle claim set, so teardown blocks until the
    repair's adoption+commit finish, and no stale/orphan attachment remains."""
    from cli_agent_orchestrator.services import callback_recovery, native_attachment_recovery

    _seed_legacy_all("claude_code")
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())
    harness.block = threading.Event()

    # Teardown observes the owning process as gone, so its release resolves.
    monkeypatch.setattr(
        native_attachment_recovery,
        "observe_owner",
        lambda record, *a, **k: {
            "disposition": "gone",
            "survivors": [],
            "observed_at": "now",
            "observer": "test",
        },
    )

    results: dict[str, Any] = {}

    def _repair() -> None:
        results["repair"] = nsr.repair_terminal_native_identity(
            terminal_id=TERMINAL_ID,
            generation=None,
            provider_version=CLAUDE_VERSION,
            physical_occurrence=CALLBACK_TARGET,
            operation_id=_uuid(),
        )

    repair_thread = threading.Thread(target=_repair, daemon=True)
    repair_thread.start()
    # Wait until the repair is holding the claims and blocked mid-capture.
    deadline = threading.Event()
    while not deadline.is_set():
        if harness.calls.count("capture") > 0:
            break
        deadline.wait(timeout=0.02)

    teardown_started = threading.Event()
    teardown_done = threading.Event()

    def _teardown() -> None:
        snapshot = {
            "id": TERMINAL_ID,
            "generation": None,
            "callback_target_generation": CALLBACK_TARGET,
            "pane_id": PANE_ID,
        }
        with callback_recovery.generation_lifecycle_claims(
            callback_recovery.terminal_lifecycle_claim_set(snapshot)
        ):
            teardown_started.set()
            roster.retire_incarnation(terminal_id=TERMINAL_ID, generation=None, reason="stop")
            native_attachment_recovery.release_owned_by_terminal(TERMINAL_ID, generation=None)
        teardown_done.set()

    teardown_thread = threading.Thread(target=_teardown, daemon=True)
    teardown_thread.start()

    # The teardown cannot acquire the shared claims while the repair holds
    # them (it would retire/release concurrently otherwise).
    assert teardown_started.wait(timeout=0.3) is False

    harness.block.set()
    repair_thread.join(timeout=30)
    teardown_thread.join(timeout=30)

    assert results["repair"]["status"] == "repaired"
    assert teardown_done.is_set()
    # The repair adopted the attachment; teardown then released it.  No
    # stale/orphan ATTACHED row survives.
    attachment = native_attachment.get("claude_code", SESSION_ID)
    assert attachment is not None
    assert attachment["state"] == native_attachment.DETACHED
    with database.SessionLocal() as db:
        inc = (
            db.query(database.StableAgentIncarnationModel)
            .filter(
                database.StableAgentIncarnationModel.terminal_id == TERMINAL_ID,
                database.StableAgentIncarnationModel.generation.is_(None),
            )
            .one()
        )
        assert inc.disposition == roster.INCARNATION_RETIRED


# ---------------------------------------------------------------------------
# Claude Escape exactly once across every failure class (F)
# ---------------------------------------------------------------------------


def _assert_escape_contract(
    harness: _RepairHarness,
    outcome: Optional[dict[str, Any]],
    *,
    expected_status: Optional[str],
    expected_reason: Optional[str],
) -> None:
    assert _typed_bytes(harness) == [
        ("literal", "/status"),
        ("enter", ""),
        ("key", "Escape"),
    ], harness.typed
    assert harness.escapes == 1
    assert harness.lease_held_at_escape is True
    if outcome is not None:
        assert outcome["status"] == expected_status
        assert outcome["reason"] == expected_reason
    with pia.pane_input_lease(PANE_ID, holder="test", timeout=0.0):
        pass


def _claude_call(**changes: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "terminal_id": TERMINAL_ID,
        "generation": GENERATION,
        "provider_version": CLAUDE_VERSION,
        "operation_id": _uuid(),
    }
    payload.update(changes)
    return nsr.repair_terminal_native_identity(**payload)


def test_claude_escape_exactly_once_on_panel_timeout(isolated_memory_db, harness, monkeypatch):
    monkeypatch.setattr(v2, "NATIVE_PANE_READY_TIMEOUT_SECONDS", 0.15)
    _seed_all("claude_code")
    harness.screens.append(["garbage that never parses"])

    outcome = _claude_call()
    _assert_escape_contract(
        harness, outcome, expected_status="refused", expected_reason="panel-unparsed"
    )
    assert _terminal_row().v2_native_session_id is None


def test_claude_escape_exactly_once_on_capture_exception(isolated_memory_db, harness, monkeypatch):
    monkeypatch.setattr(v2, "NATIVE_PANE_READY_TIMEOUT_SECONDS", 0.15)
    _seed_all("claude_code")
    harness.capture_errors.append(RuntimeError("capture exploded"))

    outcome = _claude_call()
    _assert_escape_contract(
        harness, outcome, expected_status="refused", expected_reason="panel-unparsed"
    )
    assert _terminal_row().v2_native_session_id is None


def test_claude_identity_conflict_before_bytes_when_durable_ids_disagree(
    isolated_memory_db, harness
):
    # The managed binding names SESSION_ID while the roster lineage is bound
    # to a different id: any disagreement among durable known identities is
    # refused before a single pane byte.
    _seed_all("claude_code")
    roster.record_native_identity(
        terminal_id=TERMINAL_ID,
        native_session_id="11111111-2222-4333-8444-555555555555",
        harness="claude_code",
        generation=GENERATION,
        acquisition_method=native_attachment.ACQUISITION_STATUS_DISCOVERED,
    )
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())

    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "identity-conflict"
    assert harness.typed == []
    assert _current_lineage()["native_session_id"] == "11111111-2222-4333-8444-555555555555"


def test_claude_escape_exactly_once_on_attachment_conflict(isolated_memory_db, harness):
    _seed_all("claude_code")
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())
    _seed_terminal("claude_code", terminal_id="d4e5f607", generation=_uuid())
    _declare_attachment("claude_code", SESSION_ID, terminal_id="d4e5f607", generation=_uuid())

    outcome = _claude_call()
    _assert_escape_contract(
        harness, outcome, expected_status="refused", expected_reason="attachment-conflict"
    )
    assert _terminal_row().v2_native_session_id is None
    assert _current_lineage()["native_session_id"] is None
    assert _evidence_rows() == []


def test_claude_escape_exactly_once_when_composer_proof_fails(isolated_memory_db, harness):
    _seed_all("claude_code")
    harness.screens.append(claude_panel_rows())
    harness.composer_proof_rows = ["still showing the modal", "Esc to cancel"]

    outcome = _claude_call()
    _assert_escape_contract(
        harness, outcome, expected_status="refused", expected_reason="composer-not-restored"
    )
    assert _terminal_row().v2_native_session_id is None
    assert _evidence_rows() == []


def test_claude_escape_exactly_once_on_persistence_failure(
    isolated_memory_db, harness, monkeypatch
):
    _seed_all("claude_code")
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())
    monkeypatch.setattr(
        database,
        "set_terminal_native_session_id_conditional",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db exploded")),
    )

    outcome = _claude_call()
    _assert_escape_contract(
        harness, outcome, expected_status="refused", expected_reason="persistence-failed"
    )
    attachment = native_attachment.get("claude_code", SESSION_ID)
    assert attachment is not None and attachment["state"] == native_attachment.ATTACHED
    assert _terminal_row().v2_native_session_id is None


def test_claude_escape_exactly_once_on_cancellation(isolated_memory_db, harness):
    _seed_all("claude_code")
    harness.capture_errors.append(asyncio.CancelledError("cancelled"))

    with pytest.raises(asyncio.CancelledError):
        _claude_call()
    # Cancellation never releases the claims/lease before provider cleanup:
    # the Escape ran under the held lease.
    _assert_escape_contract(harness, None, expected_status=None, expected_reason=None)
    assert _terminal_row().v2_native_session_id is None


def test_cleanup_failure_never_turns_primary_failure_into_success(
    isolated_memory_db, harness, monkeypatch
):
    _seed_all("claude_code")
    monkeypatch.setattr(v2, "NATIVE_PANE_READY_TIMEOUT_SECONDS", 0.15)
    harness.screens.append(["garbage that never parses"])

    def _key_failure(self, keystroke):
        self._state.typed_key(keystroke)
        raise RuntimeError("Escape refused by tmux")

    monkeypatch.setattr(_FakeTmuxPaneInput, "send_key", _key_failure)

    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "panel-unparsed"
    assert harness.escapes == 1
    assert _terminal_row().v2_native_session_id is None


def test_escape_failure_alone_never_reports_success(isolated_memory_db, harness, monkeypatch):
    _seed_all("claude_code")
    harness.screens.append(claude_panel_rows())

    def _key_failure(self, keystroke):
        self._state.typed_key(keystroke)
        raise RuntimeError("Escape refused by tmux")

    monkeypatch.setattr(_FakeTmuxPaneInput, "send_key", _key_failure)

    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "composer-not-restored"
    assert harness.escapes == 1
    assert _terminal_row().v2_native_session_id is None
    assert _current_lineage()["native_session_id"] is None
    assert _evidence_rows() == []


# ---------------------------------------------------------------------------
# Contention, drift, and idempotence
# ---------------------------------------------------------------------------


def test_lease_contention_writes_zero_bytes_and_zero_mutation(isolated_memory_db, harness):
    _seed_all("claude_code")
    harness.screens.append(claude_panel_rows())
    held = threading.Event()
    release = threading.Event()

    def _holder() -> None:
        with pia.pane_input_lease(PANE_ID, holder="other-writer", timeout=0.0):
            held.set()
            release.wait(timeout=10)

    holder = threading.Thread(target=_holder, daemon=True)
    holder.start()
    try:
        held.wait(timeout=10)
        outcome = _claude_call()
    finally:
        release.set()
        holder.join(timeout=10)
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "pane-busy"
    assert harness.typed == []
    assert _terminal_row().v2_native_session_id is None


def test_provider_active_writes_zero_bytes(isolated_memory_db, harness):
    _seed_all("claude_code")
    harness.turn_states = [TerminalStatus.PROCESSING]
    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "not-ready"
    assert harness.typed == []
    assert _terminal_row().v2_native_session_id is None


@pytest.mark.parametrize(
    "mutate, expected_reason",
    [
        pytest.param(
            lambda: _seed_terminal("claude_code", generation=_uuid()),
            "generation-mismatch",
            id="generation-drift",
        ),
        pytest.param(
            lambda: _seed_terminal("claude_code", lifecycle="dead"),
            "terminal-not-live",
            id="lifecycle-drift",
        ),
        pytest.param(
            lambda: _seed_terminal("claude_code", pane_pid=9999),
            "pane-identity-drift",
            id="pane-pid-drift",
        ),
        pytest.param(
            lambda: _seed_terminal("claude_code", server_socket="/tmp/other.sock"),
            "server-identity-drift",
            id="server-socket-drift",
        ),
    ],
)
def test_drift_before_any_bytes_is_refused(isolated_memory_db, harness, mutate, expected_reason):
    _seed_roster("claude_code")
    mutate()
    harness.screens.append(claude_panel_rows())

    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == expected_reason
    assert harness.typed == []
    assert _terminal_row().v2_native_session_id is None


def test_process_identity_drift_is_refused(isolated_memory_db, harness):
    _seed_terminal("claude_code")
    _seed_roster("claude_code", start_marker="Mon Jan 1 00:00:00 2024")
    harness.screens.append(claude_panel_rows())

    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "process-identity-drift"
    assert harness.typed == []


def test_retired_or_missing_roster_incarnation_refuses(isolated_memory_db, harness):
    _seed_terminal("claude_code")
    _seed_roster("claude_code")
    roster.retire_incarnation(terminal_id=TERMINAL_ID, generation=GENERATION, reason="stop")
    harness.screens.append(claude_panel_rows())
    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "incarnation-retired"
    assert harness.typed == []

    orphan_id, orphan_gen = "e5f60708", _uuid()
    _seed_terminal("codex", terminal_id=orphan_id, generation=orphan_gen)
    outcome = nsr.repair_terminal_native_identity(
        terminal_id=orphan_id,
        generation=orphan_gen,
        provider_version=CODEX_VERSION,
        operation_id=_uuid(),
    )
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "no-roster-incarnation"


def test_unsupported_build_and_provider_refuse_before_any_io(isolated_memory_db, harness):
    # On a v2 terminal the durable binding is authoritative: a caller build
    # that disagrees with it is version-drift before any I/O.
    _seed_all("claude_code")
    outcome = _claude_call(provider_version="9.9.9")
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "version-drift"
    assert harness.typed == []

    # On a legacy row with no durable binding, an unsupported build is the
    # fail-closed refusal.
    legacy_id = "c3d4e5f6"
    _seed_legacy("claude_code", terminal_id=legacy_id)
    _seed_roster("claude_code", generation=None, terminal_id=legacy_id)
    outcome = nsr.repair_terminal_native_identity(
        terminal_id=legacy_id,
        generation=None,
        physical_occurrence=CALLBACK_TARGET,
        provider_version="9.9.9",
        operation_id=_uuid(),
    )
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "unsupported-build"
    assert harness.typed == []

    other_id, other_gen = "f6070819", _uuid()
    _seed_terminal("kiro_cli", terminal_id=other_id, generation=other_gen)
    outcome = nsr.repair_terminal_native_identity(
        terminal_id=other_id,
        generation=other_gen,
        provider_version="1.0.0",
        operation_id=_uuid(),
    )
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "provider-unsupported"
    assert harness.typed == []


def test_stored_different_id_is_never_overwritten(isolated_memory_db, harness):
    # Both sides know different ids: a typed conflict with zero bytes.
    _seed_terminal("claude_code", native_session_id="11111111-2222-4333-8444-555555555555")
    _seed_roster("claude_code", native_session_id="22222222-2222-4222-8222-222222222222")

    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "identity-conflict"
    assert harness.typed == []
    assert _terminal_row().v2_native_session_id == "11111111-2222-4333-8444-555555555555"
    assert _evidence_rows() == []


# ---------------------------------------------------------------------------
# C: known-identity preflight before bytes
# ---------------------------------------------------------------------------


def test_both_known_and_equal_with_attachment_is_already_known_zero_bytes(
    isolated_memory_db, harness
):
    _seed_terminal("claude_code", native_session_id=SESSION_ID)
    _seed_roster("claude_code", native_session_id=SESSION_ID)
    _seed_exact_attached_attachment(
        "claude_code", SESSION_ID, terminal_id=TERMINAL_ID, generation=GENERATION
    )

    outcome = _claude_call()
    assert outcome["status"] == "already-known"
    assert outcome["native_session_id"] == SESSION_ID
    assert harness.typed == []
    assert _evidence_rows() == []
    assert _terminal_row().v2_native_session_id == SESSION_ID


def test_both_known_but_conflicting_is_a_typed_conflict_zero_bytes(isolated_memory_db, harness):
    _seed_terminal("claude_code", native_session_id="11111111-2222-4333-8444-555555555555")
    _seed_roster("claude_code", native_session_id="22222222-2222-4222-8222-222222222222")

    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "identity-conflict"
    assert harness.typed == []
    assert _terminal_row().v2_native_session_id == "11111111-2222-4333-8444-555555555555"


def test_both_known_equal_with_no_attachment_is_attachment_unresolved(isolated_memory_db, harness):
    _seed_terminal("claude_code", native_session_id=SESSION_ID)
    _seed_roster("claude_code", native_session_id=SESSION_ID)

    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "attachment-unresolved"
    assert harness.typed == []
    assert _evidence_rows() == []


def test_terminal_only_known_must_match_the_panel(isolated_memory_db, harness):
    # Legacy row (no binding) with exactly one known durable source: the
    # terminal native id.  The panel must attest it.
    _seed_legacy("claude_code", native_session_id=SESSION_ID)
    _seed_roster("claude_code", generation=None)
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())

    outcome = _claude_call(generation=None, physical_occurrence=CALLBACK_TARGET)
    assert outcome["status"] == "repaired"
    assert outcome["native_session_id"] == SESSION_ID


def test_terminal_only_known_mismatch_is_a_typed_refusal_with_durable_unchanged(
    isolated_memory_db, harness
):
    _seed_legacy("claude_code", native_session_id=SESSION_ID)
    _seed_roster("claude_code", generation=None)
    harness.screens.append(claude_panel_rows(session_id="11111111-2222-4333-8444-555555555555"))
    harness.styled_screens.append(claude_composer_rows())

    outcome = _claude_call(generation=None, physical_occurrence=CALLBACK_TARGET)
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "identity-conflict"
    assert _legacy_row().native_session_id == SESSION_ID
    assert _current_lineage(generation=None)["native_session_id"] is None
    assert _evidence_rows() == []


def test_lineage_only_known_must_match_the_panel(isolated_memory_db, harness):
    _seed_legacy("claude_code")
    _seed_roster("claude_code", native_session_id=SESSION_ID, generation=None)
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())

    outcome = _claude_call(generation=None, physical_occurrence=CALLBACK_TARGET)
    assert outcome["status"] == "repaired"
    assert outcome["native_session_id"] == SESSION_ID


def test_kimi_still_missing_cannot_silently_ignore_a_known_id(isolated_memory_db, harness):
    # A known id exists (only the terminal source) but the Kimi panel
    # renders no session: the known id could not be verified, so it is a
    # typed refusal with durable unchanged.  A legacy Kimi row keeps the
    # binding out of the picture so exactly one durable source is known.
    _seed_legacy("kimi_cli", native_session_id=KIMI_SESSION_ID)
    _seed_roster("kimi_cli", generation=None)
    harness.screens.append(kimi_panel_rows(session_id=None))

    outcome = _call(
        provider_version=KIMI_VERSION,
        generation=None,
        physical_occurrence=CALLBACK_TARGET,
    )
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "identity-conflict"
    assert _legacy_row().native_session_id == KIMI_SESSION_ID
    assert _evidence_rows() == []


def test_lineage_only_known_mismatch_is_a_typed_refusal_with_durable_unchanged(
    isolated_memory_db, harness
):
    _seed_legacy("claude_code")
    _seed_roster("claude_code", native_session_id=SESSION_ID, generation=None)
    harness.screens.append(claude_panel_rows(session_id="11111111-2222-4333-8444-555555555555"))
    harness.styled_screens.append(claude_composer_rows())

    outcome = _claude_call(generation=None, physical_occurrence=CALLBACK_TARGET)
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "identity-conflict"
    assert _legacy_row().native_session_id is None
    assert _current_lineage(generation=None)["native_session_id"] == SESSION_ID
    assert _evidence_rows() == []


# ---------------------------------------------------------------------------
# D: operation-id idempotency
# ---------------------------------------------------------------------------


def test_exact_retry_adopts_the_recorded_evidence_without_second_status(
    isolated_memory_db, harness
):
    _seed_all("claude_code")
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())
    op = _uuid()

    first = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        provider_version=CLAUDE_VERSION,
        operation_id=op,
    )
    assert first["status"] == "repaired"
    harness.typed.clear()
    harness.calls.clear()

    # A response-loss-style exact retry: same operation id, identical inputs.
    second = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        provider_version=CLAUDE_VERSION,
        operation_id=op,
    )
    assert second["status"] == "repaired"
    assert second["native_session_id"] == SESSION_ID
    assert second["evidence_sha256"] == first["evidence_sha256"]
    assert harness.typed == [], harness.typed
    assert len(_evidence_rows()) == 1


def test_same_operation_id_with_changed_request_is_conflict_before_pane_io(
    isolated_memory_db, harness
):
    _seed_all("claude_code")
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())
    op = _uuid()

    first = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        provider_version=CLAUDE_VERSION,
        operation_id=op,
    )
    assert first["status"] == "repaired"
    harness.typed.clear()

    second = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        provider_version=CODEX_VERSION,
        operation_id=op,
    )
    # The caller's provider version disagrees with the durable managed-v2
    # binding: a typed version-drift refusal, never a recorded success.
    assert second["status"] == "refused"
    assert second["reason"] == "version-drift"
    assert harness.typed == []
    assert len(_evidence_rows()) == 1


def test_operation_id_is_required_and_must_be_a_canonical_uuid(isolated_memory_db, harness):
    outcome = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        provider_version=CLAUDE_VERSION,
        operation_id="",
    )
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "invalid-input"

    outcome = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        provider_version=CLAUDE_VERSION,
        operation_id="not-a-uuid",
    )
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "invalid-input"
    assert harness.typed == []


# ---------------------------------------------------------------------------
# E: redaction and bounded errors
# ---------------------------------------------------------------------------


def test_secret_sentinel_in_malformed_pane_text_is_absent_from_the_result(
    isolated_memory_db, harness
):
    _seed_all("claude_code")
    # A malformed panel that carries the secret in every row.
    harness.screens.append([f"Session: {SECRET}", f"Model: {SECRET}"])
    outcome = _claude_call()
    assert outcome["status"] == "refused"
    text = str(outcome)
    assert SECRET not in text
    # The detail is bounded and typed, never raw pane text.
    assert len(outcome.get("detail") or "") <= 500


def test_unexpected_failure_detail_is_bounded_and_does_not_leak_exceptions(
    isolated_memory_db, harness, monkeypatch
):
    _seed_all("claude_code")
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())
    monkeypatch.setattr(
        database,
        "set_terminal_native_session_id_conditional",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError(SECRET + " internal detail")),
    )
    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "persistence-failed"
    assert SECRET not in str(outcome)


# ---------------------------------------------------------------------------
# Attachment adoption contract (G: detached CAS regression)
# ---------------------------------------------------------------------------


def test_attachment_other_live_owner_refuses_before_identity_mutation(isolated_memory_db, harness):
    _seed_all("claude_code")
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())
    _seed_terminal("claude_code", terminal_id="d4e5f607", generation=_uuid())
    _declare_attachment("claude_code", SESSION_ID, terminal_id="d4e5f607", generation=_uuid())

    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "attachment-conflict"
    assert _terminal_row().v2_native_session_id is None
    assert _current_lineage()["native_session_id"] is None


def test_attachment_frozen_ambiguous_refuses(isolated_memory_db, harness):
    _seed_all("claude_code")
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())
    _seed_terminal("claude_code", terminal_id="d4e5f607", generation=_uuid())
    _declare_attachment("claude_code", SESSION_ID, terminal_id="d4e5f607", generation=_uuid())
    native_attachment.mark_ambiguous(
        provider="claude_code", native_session_id=SESSION_ID, reason="operator froze it"
    )

    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "attachment-conflict"
    assert _terminal_row().v2_native_session_id is None


def test_attachment_exact_same_owner_adopts_idempotently(isolated_memory_db, harness):
    _seed_all("claude_code")
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())

    first = _claude_call()
    # The second independent repair finds the identity already known and
    # attached: a typed no-op, one owner, one receipt (the first).
    second = _claude_call()
    assert first["status"] == "repaired"
    assert second["status"] == "already-known"
    assert first["operation_id"] != second["operation_id"]
    attachment = native_attachment.get("claude_code", SESSION_ID)
    assert attachment["owner"]["terminal_id"] == TERMINAL_ID
    assert attachment["adoption_receipt"]["operation_id"] == first["operation_id"]
    assert len(_evidence_rows()) == 1


def test_detached_attachment_re_adoption_wins_the_epoch_cas(isolated_memory_db, harness):
    """A released row is re-adopted only by winning the CAS on its exact
    observed epoch; a concurrent re-acquirer loses visibly and the release
    proof is preserved."""
    _seed_terminal("claude_code")
    _seed_roster("claude_code")
    process_identity = {"pid": PANE_PID, "start_marker": START_MARKER}
    op = _uuid()
    digest = "b" * 64

    # Declare and release (detach) a claim for this session.
    native_attachment.declare(
        provider="claude_code",
        native_session_id=SESSION_ID,
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        execution_mode=em.NATIVE_TUI,
        intent=native_attachment.acquire_intent(
            acquisition_method=native_attachment.ACQUISITION_CHOSEN_SESSION_ID,
            acquisition_receipt={"schema": "test-intent"},
            admits_only_new_instructions=True,
            replays_task_bytes=False,
        ),
    )
    native_attachment.mark_starting(
        provider="claude_code",
        native_session_id=SESSION_ID,
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        execution_mode=em.NATIVE_TUI,
    )
    native_attachment.mark_attached(
        provider="claude_code",
        native_session_id=SESSION_ID,
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        execution_mode=em.NATIVE_TUI,
        process_identity=process_identity,
        pane_id=PANE_ID,
    )
    native_attachment.release(
        provider="claude_code",
        native_session_id=SESSION_ID,
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        execution_mode=em.NATIVE_TUI,
        proof=native_attachment.no_survivor_proof(
            provider="claude_code",
            native_session_id=SESSION_ID,
            terminal_id=TERMINAL_ID,
            generation=GENERATION,
            execution_mode=em.NATIVE_TUI,
            pane_id=PANE_ID,
            process_identity=process_identity,
            survivors=[],
            observed_at="now",
            observer="test",
        ),
    )
    assert native_attachment.get("claude_code", SESSION_ID)["state"] == native_attachment.DETACHED

    receipt = native_attachment.status_repair_adoption_receipt(
        operation_id=op,
        request_digest="a" * 64,
        provider="claude_code",
        native_session_id=SESSION_ID,
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        execution_mode=em.NATIVE_TUI,
        pane_id=PANE_ID,
        process_identity=process_identity,
        parser_key="claude-modal-v1",
        provider_version=CLAUDE_VERSION,
        evidence_sha256=digest,
        observed_at="now",
        composer_restored=True,
    )
    intent = native_attachment.acquire_intent(
        acquisition_method=native_attachment.ACQUISITION_STATUS_DISCOVERED,
        acquisition_receipt={"schema": "test", "native_session_id": SESSION_ID},
        admits_only_new_instructions=True,
        replays_task_bytes=False,
    )

    record, adopted = native_attachment.adopt_running_owner(
        provider="claude_code",
        native_session_id=SESSION_ID,
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        execution_mode=em.NATIVE_TUI,
        pane_id=PANE_ID,
        process_identity=process_identity,
        receipt=receipt,
        intent=intent,
    )
    assert adopted is True
    assert record["state"] == native_attachment.ATTACHED
    # The release proof is preserved as evidence.
    assert record["release_proof"] is not None
    # The receipt validated against the exact owner.
    assert record["adoption_receipt"]["operation_id"] == op

    # A same-owner re-adoption is idempotent (receipt untouched).
    record2, adopted2 = native_attachment.adopt_running_owner(
        provider="claude_code",
        native_session_id=SESSION_ID,
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        execution_mode=em.NATIVE_TUI,
        pane_id=PANE_ID,
        process_identity=process_identity,
        receipt=receipt,
        intent=intent,
    )
    assert adopted2 is False
    assert record2["adoption_receipt"]["operation_id"] == op


def test_detached_re_adoption_refuses_a_concurrent_winner_visibly(isolated_memory_db, harness):
    """A concurrent re-acquirer that re-claims the released session between
    the observation and the adoption wins the CAS; the repair's adoption
    then loses visibly (typed conflict) instead of overwriting the winner."""
    _seed_terminal("claude_code")
    _seed_roster("claude_code")
    process_identity = {"pid": PANE_PID, "start_marker": START_MARKER}

    native_attachment.declare(
        provider="claude_code",
        native_session_id=SESSION_ID,
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        execution_mode=em.NATIVE_TUI,
        intent=native_attachment.acquire_intent(
            acquisition_method=native_attachment.ACQUISITION_CHOSEN_SESSION_ID,
            acquisition_receipt={"schema": "test-intent"},
            admits_only_new_instructions=True,
            replays_task_bytes=False,
        ),
    )
    native_attachment.mark_starting(
        provider="claude_code",
        native_session_id=SESSION_ID,
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        execution_mode=em.NATIVE_TUI,
    )
    native_attachment.mark_attached(
        provider="claude_code",
        native_session_id=SESSION_ID,
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        execution_mode=em.NATIVE_TUI,
        process_identity=process_identity,
        pane_id=PANE_ID,
    )
    native_attachment.release(
        provider="claude_code",
        native_session_id=SESSION_ID,
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        execution_mode=em.NATIVE_TUI,
        proof=native_attachment.no_survivor_proof(
            provider="claude_code",
            native_session_id=SESSION_ID,
            terminal_id=TERMINAL_ID,
            generation=GENERATION,
            execution_mode=em.NATIVE_TUI,
            pane_id=PANE_ID,
            process_identity=process_identity,
            survivors=[],
            observed_at="now",
            observer="test",
        ),
    )
    # A concurrent re-acquirer re-claims the released session (a different
    # owner), which wins the detached re-acquire CAS.
    native_attachment.declare(
        provider="claude_code",
        native_session_id=SESSION_ID,
        terminal_id="d4e5f607",
        generation=_uuid(),
        execution_mode=em.NATIVE_TUI,
        intent=native_attachment.acquire_intent(
            acquisition_method=native_attachment.ACQUISITION_CHOSEN_SESSION_ID,
            acquisition_receipt={"schema": "test-intent"},
            admits_only_new_instructions=True,
            replays_task_bytes=False,
        ),
    )
    receipt = native_attachment.status_repair_adoption_receipt(
        operation_id=_uuid(),
        request_digest="a" * 64,
        provider="claude_code",
        native_session_id=SESSION_ID,
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        execution_mode=em.NATIVE_TUI,
        pane_id=PANE_ID,
        process_identity=process_identity,
        parser_key="claude-modal-v1",
        provider_version=CLAUDE_VERSION,
        evidence_sha256="b" * 64,
        observed_at="now",
        composer_restored=True,
    )
    intent = native_attachment.acquire_intent(
        acquisition_method=native_attachment.ACQUISITION_STATUS_DISCOVERED,
        acquisition_receipt={"schema": "test", "native_session_id": SESSION_ID},
        admits_only_new_instructions=True,
        replays_task_bytes=False,
    )
    with pytest.raises(native_attachment.NativeAttachmentConflict):
        native_attachment.adopt_running_owner(
            provider="claude_code",
            native_session_id=SESSION_ID,
            terminal_id=TERMINAL_ID,
            generation=GENERATION,
            execution_mode=em.NATIVE_TUI,
            pane_id=PANE_ID,
            process_identity=process_identity,
            receipt=receipt,
            intent=intent,
        )
    # The concurrent winner's claim is untouched.
    assert native_attachment.get("claude_code", SESSION_ID)["owner"]["terminal_id"] == "d4e5f607"


def test_failure_after_attachment_is_conservative_and_exact_retry_converges(
    isolated_memory_db, harness, monkeypatch
):
    _seed_all("claude_code")
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())
    op = _uuid()
    real_commit = nsr._commit_repair
    calls = {"n": 0}

    def _fail_once(db, facts):
        calls["n"] += 1
        if calls["n"] == 1:
            raise nsr.NativeStatusRepairUnavailable("row+roster commit failed")
        return real_commit(db, facts)

    monkeypatch.setattr(nsr, "_commit_repair", _fail_once)

    first = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        provider_version=CLAUDE_VERSION,
        operation_id=op,
    )
    assert first["status"] == "refused"
    assert first["reason"] == "persistence-failed"
    attachment = native_attachment.get("claude_code", SESSION_ID)
    assert attachment is not None and attachment["state"] == native_attachment.ATTACHED
    assert _terminal_row().v2_native_session_id is None

    # An exact retry converges without another /status via the validated
    # prior receipt.
    harness.typed.clear()
    harness.calls.clear()
    second = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        provider_version=CLAUDE_VERSION,
        operation_id=op,
    )
    assert second["status"] == "repaired"
    assert harness.typed == [], harness.typed
    assert _terminal_row().v2_native_session_id == SESSION_ID
    assert _current_lineage()["native_session_id"] == SESSION_ID


# ---------------------------------------------------------------------------
# Transaction rollback and no-side-effect guarantees
# ---------------------------------------------------------------------------


def test_transaction_rollback_leaves_neither_side_repaired(
    isolated_memory_db, harness, monkeypatch
):
    _seed_all("claude_code")
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())

    real_record = roster.record_native_identity

    def _fail_roster(**kwargs):
        if kwargs.get("db") is not None:
            raise roster.StableAgentUnavailable("roster store exploded")
        return real_record(**kwargs)

    monkeypatch.setattr(roster, "record_native_identity", _fail_roster)
    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "persistence-failed"
    assert _terminal_row().v2_native_session_id is None
    assert _evidence_rows() == []
    assert _current_lineage()["native_session_id"] is None


def test_operation_never_touches_teardown_or_delivery(isolated_memory_db, harness, monkeypatch):
    from cli_agent_orchestrator.services import terminal_service

    _seed_all("claude_code")
    harness.screens.append(claude_panel_rows())

    def _loud(*args, **kwargs):
        raise AssertionError("the repair must never touch teardown or delivery")

    monkeypatch.setattr(terminal_service, "delete_terminal", _loud)
    monkeypatch.setattr(native_attachment, "release", _loud)
    monkeypatch.setattr(native_attachment, "mark_ambiguous", _loud)
    monkeypatch.setattr(native_attachment, "mark_draining", _loud)

    outcome = _claude_call()
    assert outcome["status"] == "refused"  # the unparseable panel is refused
    with pia.pane_input_lease(PANE_ID, holder="teardown-check", timeout=0.0):
        pass


def test_terminal_not_found_is_typed(isolated_memory_db, harness):
    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "terminal-not-found"
    assert harness.typed == []


# ---------------------------------------------------------------------------
# Follow-up 2: fixes 1-9
# ---------------------------------------------------------------------------


# --- 1: true legacy callback target is never mutated while refusing ---


def test_true_legacy_missing_callback_target_refuses_without_mutating(
    isolated_memory_db, harness, monkeypatch
):
    """A generation-null legacy row with no callback target refuses without
    calling (or mutating through) the get_terminal_metadata self-heal."""
    with database.SessionLocal() as db:
        db.add(
            database.TerminalModel(
                id=TERMINAL_ID,
                tmux_session=SESSION_NAME,
                tmux_window=f"w-{TERMINAL_ID}",
                provider="claude_code",
                generation=None,
                callback_target_generation=None,
                pane_id=PANE_ID,
                window_id=WINDOW_ID,
                server_socket_path=SERVER_SOCKET,
                session_id=TMUX_SESSION_ID,
                pane_pid=PANE_PID,
                lifecycle_state="live",
            )
        )
        db.commit()

    def _loud(*args, **kwargs):
        raise AssertionError("the self-heal seam must not be called for a true legacy row")

    monkeypatch.setattr(database, "get_terminal_metadata", _loud)
    outcome = _call(generation=None)
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "callback-target-missing"
    assert harness.typed == []
    with database.SessionLocal() as db:
        row = db.query(database.TerminalModel).filter_by(id=TERMINAL_ID).one()
        assert row.callback_target_generation is None


# --- 2: use only the terminal native ID re-read under claims ---


def _mutate_terminal_and_lineage_before_claim(monkeypatch, terminal_id, lineage_id):
    """Make the pre-claim roster read change the terminal + lineage native
    ids, simulating a concurrent change between the initial load and the
    lifecycle claim."""
    real_get = roster.get_incarnation_by_terminal

    def _mutating_get(terminal, generation=None, db=None):
        result = real_get(terminal, generation=generation, db=db)
        if db is None:  # the pre-claim roster read
            with database.SessionLocal() as s:
                v2 = s.query(database.ManagedLaunchV2TerminalModel).filter_by(id=terminal).first()
                if v2 is not None:
                    v2.v2_native_session_id = terminal_id
                inc = (
                    s.query(database.StableAgentIncarnationModel)
                    .filter_by(terminal_id=terminal)
                    .first()
                )
                lineage = (
                    s.query(database.StableAgentLineageModel)
                    .filter_by(lineage_id=inc.lineage_id)
                    .first()
                )
                lineage.native_session_id = lineage_id
                s.commit()
        return result

    monkeypatch.setattr(roster, "get_incarnation_by_terminal", _mutating_get)


def test_concurrent_terminal_id_change_before_claim_yields_already_known(
    isolated_memory_db, harness, monkeypatch
):
    """A concurrent change that makes terminal + lineage hold the same known
    id before the claim is acquired produces the zero-byte already-known
    outcome, not a stale pane interaction."""
    _seed_terminal("claude_code")
    _seed_roster("claude_code")
    _seed_exact_attached_attachment(
        "claude_code", SESSION_ID, terminal_id=TERMINAL_ID, generation=GENERATION
    )
    _mutate_terminal_and_lineage_before_claim(monkeypatch, SESSION_ID, SESSION_ID)

    outcome = _claude_call()
    assert outcome["status"] == "already-known"
    assert harness.typed == []
    assert _evidence_rows() == []


def test_concurrent_terminal_id_change_before_claim_yields_conflict_zero_bytes(
    isolated_memory_db, harness, monkeypatch
):
    _seed_terminal("claude_code")
    _seed_roster("claude_code")
    _mutate_terminal_and_lineage_before_claim(
        monkeypatch, "11111111-2222-4333-8444-555555555555", "22222222-2222-4222-8222-222222222222"
    )

    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "identity-conflict"
    assert harness.typed == []
    assert _evidence_rows() == []
    assert native_attachment.list_attachments(owner_terminal_id=TERMINAL_ID) == []


def test_drift_after_observation_before_adoption_refuses_without_adopting(
    isolated_memory_db, harness, monkeypatch
):
    """A terminal native id that changes after the panel observation but
    before attachment adoption is refused; a wrong conservative claim is
    never adopted."""
    _seed_all("claude_code")
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())
    real_verdict = nsr._capture_panel_verdict

    def _mutating_verdict(provider, pane_id, plan, **kwargs):
        verdict = real_verdict(provider, pane_id, plan, **kwargs)
        with database.SessionLocal() as s:
            row = s.query(database.ManagedLaunchV2TerminalModel).filter_by(id=TERMINAL_ID).first()
            row.v2_native_session_id = "99999999-8888-4777-8666-555555555555"
            s.commit()
        return verdict

    monkeypatch.setattr(nsr, "_capture_panel_verdict", _mutating_verdict)
    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "identity-conflict"
    assert _terminal_row().v2_native_session_id == "99999999-8888-4777-8666-555555555555"
    assert native_attachment.get("claude_code", SESSION_ID) is None
    assert _evidence_rows() == []


# --- 3: already-known requires the exact live attachment owner ---


@pytest.mark.parametrize(
    "mutate_owner, label",
    [
        pytest.param(lambda attrs: attrs.update(terminal_id="d4e5f607"), "wrong-terminal"),
        pytest.param(lambda attrs: attrs.update(generation=_uuid()), "wrong-occurrence"),
        pytest.param(lambda attrs: attrs.update(pane_id="%99"), "wrong-pane"),
        pytest.param(
            lambda attrs: attrs.update(process_identity={"pid": 1, "start_marker": "x"}),
            "wrong-process",
        ),
    ],
)
def test_already_known_refuses_a_wrong_owner(isolated_memory_db, harness, mutate_owner, label):
    _seed_terminal("claude_code", native_session_id=SESSION_ID)
    _seed_roster("claude_code", native_session_id=SESSION_ID)
    attrs = {
        "terminal_id": TERMINAL_ID,
        "generation": GENERATION,
        "pane_id": PANE_ID,
        "process_identity": {"pid": PANE_PID, "start_marker": START_MARKER},
    }
    mutate_owner(attrs)
    _seed_exact_attached_attachment(
        "claude_code",
        SESSION_ID,
        terminal_id=attrs["terminal_id"],
        generation=attrs["generation"],
        pane_id=attrs["pane_id"],
        process_identity=attrs["process_identity"],
    )

    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "attachment-reconcile"
    assert harness.typed == []
    assert _evidence_rows() == []


def test_already_known_refuses_a_detached_attachment(isolated_memory_db, harness):
    _seed_terminal("claude_code", native_session_id=SESSION_ID)
    _seed_roster("claude_code", native_session_id=SESSION_ID)
    identity = {"pid": PANE_PID, "start_marker": START_MARKER}
    _seed_exact_attached_attachment(
        "claude_code", SESSION_ID, terminal_id=TERMINAL_ID, generation=GENERATION
    )
    native_attachment.release(
        provider="claude_code",
        native_session_id=SESSION_ID,
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        execution_mode=em.NATIVE_TUI,
        proof=native_attachment.no_survivor_proof(
            provider="claude_code",
            native_session_id=SESSION_ID,
            terminal_id=TERMINAL_ID,
            generation=GENERATION,
            execution_mode=em.NATIVE_TUI,
            pane_id=PANE_ID,
            process_identity=identity,
            survivors=[],
            observed_at="now",
            observer="test",
        ),
    )
    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "attachment-reconcile"
    assert harness.typed == []


def test_already_known_refuses_a_wrong_execution_mode(isolated_memory_db, harness):
    _seed_terminal("claude_code", native_session_id=SESSION_ID)
    _seed_roster("claude_code", native_session_id=SESSION_ID)
    identity = {"pid": PANE_PID, "start_marker": START_MARKER}
    # Declare the claim in ACP mode (not native_tui) and attach it.
    native_attachment.declare(
        provider="claude_code",
        native_session_id=SESSION_ID,
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        execution_mode=em.ACP,
        intent=native_attachment.acquire_intent(
            acquisition_method=native_attachment.ACQUISITION_CHOSEN_SESSION_ID,
            acquisition_receipt={"schema": "test-intent"},
            admits_only_new_instructions=True,
            replays_task_bytes=False,
        ),
    )
    native_attachment.mark_starting(
        provider="claude_code",
        native_session_id=SESSION_ID,
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        execution_mode=em.ACP,
    )
    native_attachment.mark_attached(
        provider="claude_code",
        native_session_id=SESSION_ID,
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        execution_mode=em.ACP,
        process_identity=identity,
        pane_id=PANE_ID,
    )
    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "attachment-reconcile"
    assert harness.typed == []


# --- 4: recheck operation evidence after the lifecycle claims ---


def test_concurrent_exact_retry_adopts_evidence_after_the_claim(isolated_memory_db, harness):
    _seed_all("claude_code")
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())
    harness.block = threading.Event()
    op = _uuid()
    results: dict[str, Any] = {}

    def _first() -> None:
        results["first"] = nsr.repair_terminal_native_identity(
            terminal_id=TERMINAL_ID,
            generation=GENERATION,
            provider_version=CLAUDE_VERSION,
            operation_id=op,
        )

    first_thread = threading.Thread(target=_first, daemon=True)
    first_thread.start()
    while "capture" not in harness.calls:
        time.sleep(0.02)

    def _second() -> None:
        results["second"] = nsr.repair_terminal_native_identity(
            terminal_id=TERMINAL_ID,
            generation=GENERATION,
            provider_version=CLAUDE_VERSION,
            operation_id=op,
        )

    second_thread = threading.Thread(target=_second, daemon=True)
    second_thread.start()
    time.sleep(0.1)  # let the second block on the lifecycle claim

    harness.block.set()
    first_thread.join(timeout=30)
    second_thread.join(timeout=30)

    assert results["first"]["status"] == "repaired"
    assert results["second"]["status"] == "repaired"
    assert results["second"]["evidence_sha256"] == results["first"]["evidence_sha256"]
    assert "exact retry" in results["second"]["detail"]
    # Only the first typed /status; the second adopted the recorded evidence.
    assert _typed_bytes(harness) == [
        ("literal", "/status"),
        ("enter", ""),
        ("key", "Escape"),
    ]
    assert len(_evidence_rows()) == 1


def test_concurrent_semantic_equivalent_request_converges_after_the_claim(
    isolated_memory_db, harness
):
    """Two concurrent requests with the SAME effective facts (one omits the
    provider version, the other supplies the identical pinned build) are
    canonically the same operation: the second adopts the first's evidence
    instead of conflicting."""
    _seed_legacy_all("claude_code")
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())
    harness.block = threading.Event()
    op = _uuid()
    results: dict[str, Any] = {}

    def _first() -> None:
        results["first"] = nsr.repair_terminal_native_identity(
            terminal_id=TERMINAL_ID,
            generation=None,
            physical_occurrence=CALLBACK_TARGET,
            provider_version=None,
            operation_id=op,
        )

    first_thread = threading.Thread(target=_first, daemon=True)
    first_thread.start()
    while "capture" not in harness.calls:
        time.sleep(0.02)

    def _second() -> None:
        results["second"] = nsr.repair_terminal_native_identity(
            terminal_id=TERMINAL_ID,
            generation=None,
            physical_occurrence=CALLBACK_TARGET,
            provider_version=CLAUDE_VERSION,
            operation_id=op,
        )

    second_thread = threading.Thread(target=_second, daemon=True)
    second_thread.start()
    time.sleep(0.1)

    harness.block.set()
    first_thread.join(timeout=30)
    second_thread.join(timeout=30)

    assert results["first"]["status"] == "repaired"
    assert results["second"]["status"] == "repaired"
    assert results["second"]["evidence_sha256"] == results["first"]["evidence_sha256"]
    assert _typed_bytes(harness) == [
        ("literal", "/status"),
        ("enter", ""),
        ("key", "Escape"),
    ]
    assert len(_evidence_rows()) == 1


# --- 5: exact-owner partial adoption must reconcile, never re-type ---


def _partial_exact_owner(harness, receipt_mutate):
    """Create an exact-owner attachment carrying a (possibly corrupted)
    status-repair receipt, WITHOUT any committed evidence.  Inserted
    directly so a corrupted receipt that the sanctioned seam would refuse to
    write can still exist (as a legacy/buggy row could)."""
    import json as _json

    _seed_all("claude_code")
    identity = {"pid": PANE_PID, "start_marker": START_MARKER}
    op = _uuid()
    digest = "b" * 64
    receipt = native_attachment.status_repair_adoption_receipt(
        operation_id=op,
        request_digest=canonical_digest_for("claude_code", TERMINAL_ID, GENERATION),
        provider="claude_code",
        native_session_id=SESSION_ID,
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        execution_mode=em.NATIVE_TUI,
        pane_id=PANE_ID,
        process_identity=identity,
        parser_key="claude-modal-v1",
        provider_version=CLAUDE_VERSION,
        evidence_sha256=digest,
        observed_at="now",
        composer_restored=True,
    )
    receipt_mutate(receipt)
    with database.SessionLocal() as db:
        db.add(
            database.NativeSessionAttachmentModel(
                provider="claude_code",
                native_session_id=SESSION_ID,
                state=native_attachment.ATTACHED,
                owner_terminal_id=TERMINAL_ID,
                owner_generation=GENERATION,
                owner_execution_mode=em.NATIVE_TUI,
                owner_pane_id=PANE_ID,
                owner_process_identity_json=_json.dumps(identity, sort_keys=True),
                intent_json=_json.dumps({"schema": native_attachment.INTENT_SCHEMA}),
                release_proof_json=None,
                adoption_receipt_json=_json.dumps(receipt, sort_keys=True),
                ambiguity_reason=None,
                epoch=0,
                created_at="now",
                updated_at="now",
            )
        )
        db.commit()
    return op


@pytest.mark.parametrize(
    "mutate, label",
    [
        pytest.param(lambda r: r.__setitem__("schema", "wrong"), "corrupted-schema"),
        pytest.param(lambda r: r.__setitem__("request_digest", "f" * 64), "different-digest"),
        pytest.param(lambda r: r.__setitem__("operation_id", _uuid()), "different-operation"),
        pytest.param(lambda r: r.__setitem__("parser_key", "wrong-parser"), "wrong-parser"),
        pytest.param(lambda r: r.__setitem__("provider_version", "0.146.0"), "wrong-version"),
        pytest.param(lambda r: r.__setitem__("composer_restored", False), "no-composer-proof"),
        pytest.param(lambda r: r.pop("evidence_sha256"), "missing-evidence"),
    ],
)
def test_exact_owner_with_invalid_receipt_reconciles_without_second_status(
    isolated_memory_db, harness, mutate, label
):
    op = _partial_exact_owner(harness, mutate)
    outcome = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        provider_version=CLAUDE_VERSION,
        operation_id=op,
    )
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "attachment-reconcile"
    assert harness.typed == []
    assert _evidence_rows() == []


def test_exact_owner_partial_with_matching_receipt_converges_without_second_status(
    isolated_memory_db, harness, monkeypatch
):
    """An exact-owner partial adoption whose receipt fully validates for THIS
    operation converges (reuses the observed identity) with no second
    /status."""
    _seed_all("claude_code")
    identity = {"pid": PANE_PID, "start_marker": START_MARKER}
    op = _uuid()
    digest = "b" * 64
    receipt = native_attachment.status_repair_adoption_receipt(
        operation_id=op,
        request_digest=canonical_digest_for("claude_code", TERMINAL_ID, GENERATION),
        provider="claude_code",
        native_session_id=SESSION_ID,
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        execution_mode=em.NATIVE_TUI,
        pane_id=PANE_ID,
        process_identity=identity,
        parser_key="claude-modal-v1",
        provider_version=CLAUDE_VERSION,
        evidence_sha256=digest,
        observed_at="now",
        composer_restored=True,
    )
    intent = native_attachment.acquire_intent(
        acquisition_method=native_attachment.ACQUISITION_STATUS_DISCOVERED,
        acquisition_receipt={"schema": "test", "native_session_id": SESSION_ID},
        admits_only_new_instructions=True,
        replays_task_bytes=False,
    )
    native_attachment.adopt_running_owner(
        provider="claude_code",
        native_session_id=SESSION_ID,
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        execution_mode=em.NATIVE_TUI,
        pane_id=PANE_ID,
        process_identity=identity,
        receipt=receipt,
        intent=intent,
    )
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())

    outcome = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        provider_version=CLAUDE_VERSION,
        operation_id=op,
    )
    assert outcome["status"] == "repaired"
    assert harness.typed == []
    assert len(_evidence_rows()) == 1


def canonical_digest_for(
    provider: str, terminal_id: str, generation: str, physical_occurrence: Optional[str] = None
):
    """The resolved canonical digest a repair computes for a managed
    terminal carrying the default test binding (native session SESSION_ID)."""
    binding = _default_binding(provider, SESSION_ID)
    occurrence = physical_occurrence or generation
    return nsr.resolved_request_digest(
        terminal_id=terminal_id,
        model_generation=generation,
        occurrence=occurrence,
        provider=provider,
        effective_version=PINNED_VERSION[provider],
        binding_native_id=binding["native_session_id"],
    )


# --- 6: bind legacy operation ids to the physical occurrence ---


def test_legacy_request_omitting_physical_occurrence_refuses_before_pane_io(
    isolated_memory_db, harness
):
    _seed_legacy_all("claude_code")
    outcome = _call(generation=None)
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "physical-occurrence-required"
    assert harness.typed == []
    assert _legacy_row().native_session_id is None


def test_legacy_wrong_physical_occurrence_refuses_zero_byte(isolated_memory_db, harness):
    _seed_legacy_all("claude_code")
    outcome = _call(generation=None, physical_occurrence="11111111-2222-4333-8444-5555555555aa")
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "generation-mismatch"
    assert harness.typed == []
    assert _legacy_row().native_session_id is None


def test_completed_legacy_operation_not_adopted_after_callback_target_changes(
    isolated_memory_db, harness
):
    _seed_legacy_all("claude_code")
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())
    op = _uuid()
    first = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID,
        generation=None,
        physical_occurrence=CALLBACK_TARGET,
        provider_version=CLAUDE_VERSION,
        operation_id=op,
    )
    assert first["status"] == "repaired"
    # The durable callback target changes; the same exact op/occurrence
    # cannot be adopted against the recycled physical identity.
    with database.SessionLocal() as db:
        row = db.query(database.TerminalModel).filter_by(id=TERMINAL_ID).one()
        row.callback_target_generation = "22222222-2222-4222-8222-2222222222aa"
        db.commit()
    second = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID,
        generation=None,
        physical_occurrence=CALLBACK_TARGET,
        provider_version=CLAUDE_VERSION,
        operation_id=op,
    )
    assert second["status"] == "refused"
    assert second["reason"] == "generation-mismatch"
    assert _typed_bytes(harness) == [
        ("literal", "/status"),
        ("enter", ""),
        ("key", "Escape"),
    ]


def test_same_operation_id_with_changed_occurrence_is_operation_conflict(
    isolated_memory_db, harness
):
    _seed_legacy_all("claude_code")
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())
    op = _uuid()
    first = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID,
        generation=None,
        physical_occurrence=CALLBACK_TARGET,
        provider_version=CLAUDE_VERSION,
        operation_id=op,
    )
    assert first["status"] == "repaired"
    other = "22222222-2222-4222-8222-2222222222aa"
    with database.SessionLocal() as db:
        row = db.query(database.TerminalModel).filter_by(id=TERMINAL_ID).one()
        row.callback_target_generation = other
        db.commit()
    harness.typed.clear()
    second = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID,
        generation=None,
        physical_occurrence=other,
        provider_version=CLAUDE_VERSION,
        operation_id=op,
    )
    assert second["status"] == "refused"
    assert second["reason"] == "operation-conflict"
    assert harness.typed == []


# --- 7: managed-v2 binding is authoritative where available ---


def test_managed_binding_version_match_repairs(isolated_memory_db, harness):
    _seed_all("claude_code")
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())

    outcome = _claude_call()
    assert outcome["status"] == "repaired"
    assert outcome["provider_version"] == CLAUDE_VERSION


def test_managed_binding_version_caller_mismatch_refuses_zero_mutation(isolated_memory_db, harness):
    _seed_all("claude_code")
    harness.screens.append(claude_panel_rows())

    outcome = _claude_call(provider_version="2.1.225")
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "version-drift"
    assert harness.typed == []
    assert _terminal_row().v2_native_session_id is None


def test_managed_binding_version_rendered_panel_mismatch_refuses_zero_mutation(
    isolated_memory_db, harness
):
    _seed_all("claude_code")
    harness.screens.append(claude_panel_rows(version="2.1.225"))

    outcome = _claude_call(provider_version=None)
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "panel-unparsed"
    assert _typed_bytes(harness) == [
        ("literal", "/status"),
        ("enter", ""),
        ("key", "Escape"),
    ]
    assert _terminal_row().v2_native_session_id is None
    assert _evidence_rows() == []


def test_managed_v2_without_a_bound_binding_refuses(isolated_memory_db, harness):
    # A v2 terminal whose exact reservation has no binding (or no
    # reservation) fails closed: a pre-bind process is never typed into.
    _seed_terminal("claude_code", binding=None)
    _seed_roster("claude_code")
    harness.screens.append(claude_panel_rows())
    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "binding-unavailable"
    assert harness.typed == []

    with database.SessionLocal() as db:
        (
            db.query(database.ManagedLaunchV2ReservationModel)
            .filter_by(terminal_id=TERMINAL_ID)
            .delete()
        )
        db.commit()
    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "binding-unavailable"
    assert harness.typed == []


# --- 8: no raw internal exception text in public outcomes ---


def test_attachment_conflict_secret_is_absent_from_the_public_result(
    isolated_memory_db, harness, monkeypatch
):
    secret = "super_secret_owner_fact_zz9"
    _seed_all("claude_code")
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())
    real_adopt = native_attachment.adopt_running_owner

    def _exploding_adopt(**kwargs):
        raise native_attachment.NativeAttachmentConflict(f"owner terminal={secret}")

    monkeypatch.setattr(native_attachment, "adopt_running_owner", _exploding_adopt)
    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "attachment-conflict"
    assert secret not in str(outcome)


def test_roster_failure_secret_is_absent_from_the_public_result(
    isolated_memory_db, harness, monkeypatch
):
    secret = "super_secret_roster_row_zz9"
    _seed_all("claude_code")
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())
    real_record = roster.record_native_identity

    def _exploding_roster(**kwargs):
        if kwargs.get("db") is not None:
            raise roster.StableAgentUnavailable(f"store row {secret}")
        return real_record(**kwargs)

    monkeypatch.setattr(roster, "record_native_identity", _exploding_roster)
    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "persistence-failed"
    assert secret not in str(outcome)


# ---------------------------------------------------------------------------
# Follow-up 3: fixes 1-4
# ---------------------------------------------------------------------------


# --- 1: a partial receipt can never name a different native session ---


@pytest.mark.parametrize(
    "mutate_receipt, label, expect_b_absent",
    [
        pytest.param(
            lambda r: r.__setitem__("native_session_id", "99999999-8888-4777-8666-5555555555aa"),
            "receipt-native-id-B",
            True,
        ),
        pytest.param(lambda r: r.__setitem__("provider", "codex"), "receipt-provider", False),
        pytest.param(lambda r: r.__setitem__("terminal_id", "d4e5f607"), "receipt-terminal", False),
        pytest.param(lambda r: r.__setitem__("generation", _uuid()), "receipt-generation", False),
        pytest.param(lambda r: r.__setitem__("execution_mode", "acp"), "receipt-mode", False),
        pytest.param(lambda r: r.__setitem__("pane_id", "%99"), "receipt-pane", False),
        pytest.param(
            lambda r: r.__setitem__("process_identity", {"pid": 1, "start_marker": "x"}),
            "receipt-process",
            False,
        ),
        pytest.param(lambda r: r.__setitem__("evidence_sha256", "zzz"), "receipt-evidence", False),
        pytest.param(lambda r: r.__setitem__("observed_at", ""), "receipt-observed-missing", False),
    ],
)
def test_receipt_cross_check_reconciles_without_nominating_a_new_session(
    isolated_memory_db, harness, mutate_receipt, label, expect_b_absent
):
    op = _partial_exact_owner(harness, mutate_receipt)
    outcome = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        provider_version=CLAUDE_VERSION,
        operation_id=op,
    )
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "attachment-reconcile"
    assert harness.typed == []
    assert _evidence_rows() == []
    # A corrupted receipt must never nominate a new session key.
    if expect_b_absent:
        assert native_attachment.get("claude_code", "99999999-8888-4777-8666-5555555555aa") is None
    assert native_attachment.get("claude_code", SESSION_ID) is not None


# --- 2: complete managed-v2 binding is authoritative ---


def test_binding_a_panel_b_refuses_with_no_mutation(isolated_memory_db, harness):
    _seed_terminal("claude_code", binding_session_id=SESSION_ID)
    _seed_roster("claude_code")
    harness.screens.append(claude_panel_rows(session_id="11111111-2222-4333-8444-555555555555"))
    harness.styled_screens.append(claude_composer_rows())

    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "identity-conflict"
    assert _terminal_row().v2_native_session_id is None
    assert _current_lineage()["native_session_id"] is None
    assert _evidence_rows() == []
    assert native_attachment.get("claude_code", SESSION_ID) is None


def test_terminal_lineage_b_binding_a_refuses_zero_bytes(isolated_memory_db, harness):
    _seed_terminal("claude_code", native_session_id="22222222-2222-4222-8222-222222222222")
    _seed_roster("claude_code", native_session_id="22222222-2222-4222-8222-222222222222")
    # The default binding names SESSION_ID (A), disagreeing with B.
    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "identity-conflict"
    assert harness.typed == []
    assert _evidence_rows() == []


def test_binding_only_known_must_match_the_panel(isolated_memory_db, harness):
    _seed_terminal("claude_code", binding_session_id=SESSION_ID)
    _seed_roster("claude_code")
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())

    outcome = _claude_call()
    assert outcome["status"] == "repaired"
    assert outcome["native_session_id"] == SESSION_ID


@pytest.mark.parametrize(
    "binding, expected_reason",
    [
        pytest.param(
            {
                "schema": "cao-managed-v2-native-binding-v1",
                "execution_mode": "acp",
                "native_session_id": SESSION_ID,
                "provider_version": CLAUDE_VERSION,
            },
            "binding-unreadable",
            id="wrong-mode-acp",
        ),
        pytest.param(
            {
                "schema": "not-the-schema",
                "execution_mode": "native_tui",
                "native_session_id": SESSION_ID,
                "provider_version": CLAUDE_VERSION,
            },
            "binding-unreadable",
            id="wrong-schema",
        ),
        pytest.param(
            {
                "schema": "cao-managed-v2-native-binding-v1",
                "execution_mode": "native_tui",
                "provider_version": CLAUDE_VERSION,
            },
            "binding-unreadable",
            id="missing-native-id",
        ),
        pytest.param(
            {
                "schema": "cao-managed-v2-native-binding-v1",
                "execution_mode": "native_tui",
                "native_session_id": SESSION_ID,
            },
            "binding-unreadable",
            id="missing-version",
        ),
    ],
)
def test_malformed_or_incomplete_binding_fails_closed(
    isolated_memory_db, harness, binding, expected_reason
):
    _seed_terminal("claude_code", binding=binding)
    _seed_roster("claude_code")
    harness.screens.append(claude_panel_rows())
    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == expected_reason
    assert harness.typed == []


def test_binding_wrong_generation_provider_or_state_fails_closed(isolated_memory_db, harness):
    _seed_terminal("claude_code", binding=_default_binding("claude_code", SESSION_ID))
    _seed_roster("claude_code")

    def _mutate_reservation(mutator):
        with database.SessionLocal() as db:
            row = (
                db.query(database.ManagedLaunchV2ReservationModel)
                .filter_by(terminal_id=TERMINAL_ID)
                .one()
            )
            mutator(row)
            db.commit()

    # Wrong reservation generation: the exact lookup finds nothing.
    _mutate_reservation(lambda row: setattr(row, "generation", _uuid()))
    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "binding-unavailable"
    assert harness.typed == []

    # Wrong provider.
    _mutate_reservation(lambda row: setattr(row, "provider", "codex"))
    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "binding-unavailable"
    assert harness.typed == []

    # Unbound reservation state.
    _mutate_reservation(lambda row: setattr(row, "state", "reserved"))
    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "binding-unavailable"
    assert harness.typed == []


def test_binding_malformed_json_fails_closed(isolated_memory_db, harness):
    _seed_terminal("claude_code", binding=_default_binding("claude_code", SESSION_ID))
    _seed_roster("claude_code")
    with database.SessionLocal() as db:
        row = (
            db.query(database.ManagedLaunchV2ReservationModel)
            .filter_by(terminal_id=TERMINAL_ID)
            .one()
        )
        row.binding_json = "{not valid json"
        db.commit()
    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "binding-unreadable"
    assert harness.typed == []


def test_legacy_repair_ignores_an_unrelated_v2_reservation(isolated_memory_db, harness):
    # A legacy terminal (no model generation) never consumes a stale v2
    # reservation whose terminal id merely collides.
    _seed_legacy_all("claude_code")
    _seed_v2_reservation(
        "claude_code",
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        binding=_default_binding("claude_code", SESSION_ID),
    )
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())

    outcome = _call(generation=None, physical_occurrence=CALLBACK_TARGET)
    assert outcome["status"] == "repaired"


# --- 3: canonical digest over resolved facts ---


def test_managed_omitted_occurrence_matches_explicit_same_occurrence(isolated_memory_db, harness):
    _seed_all("claude_code")
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())
    op = _uuid()

    first = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        provider_version=CLAUDE_VERSION,
        operation_id=op,
    )
    assert first["status"] == "repaired"
    harness.typed.clear()

    second = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        provider_version=CLAUDE_VERSION,
        physical_occurrence=GENERATION,
        operation_id=op,
    )
    assert second["status"] == "repaired"
    assert second["evidence_sha256"] == first["evidence_sha256"]
    assert harness.typed == []
    assert len(_evidence_rows()) == 1


def test_omitted_version_matches_explicit_same_effective_version(isolated_memory_db, harness):
    _seed_legacy_all("claude_code")
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())
    op = _uuid()

    first = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID,
        generation=None,
        physical_occurrence=CALLBACK_TARGET,
        provider_version=None,
        operation_id=op,
    )
    assert first["status"] == "repaired"
    harness.typed.clear()

    second = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID,
        generation=None,
        physical_occurrence=CALLBACK_TARGET,
        provider_version=CLAUDE_VERSION,
        operation_id=op,
    )
    assert second["status"] == "repaired"
    assert second["evidence_sha256"] == first["evidence_sha256"]
    assert harness.typed == []
    assert len(_evidence_rows()) == 1


def test_managed_binding_native_id_drift_refuses_under_the_same_operation(
    isolated_memory_db, harness
):
    _seed_all("claude_code")
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())
    op = _uuid()

    first = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        provider_version=CLAUDE_VERSION,
        operation_id=op,
    )
    assert first["status"] == "repaired"
    harness.typed.clear()

    # The durable binding now names a different native session.
    with database.SessionLocal() as db:
        row = (
            db.query(database.ManagedLaunchV2ReservationModel)
            .filter_by(terminal_id=TERMINAL_ID)
            .one()
        )
        row.binding_json = json.dumps(
            _default_binding("claude_code", "99999999-8888-4777-8666-5555555555aa")
        )
        db.commit()

    second = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        provider_version=CLAUDE_VERSION,
        operation_id=op,
    )
    assert second["status"] == "refused"
    assert second["reason"] == "operation-conflict"
    assert harness.typed == []
    assert len(_evidence_rows()) == 1


# --- 4: typed/redacted attachment lookup failure ---


def test_attachment_lookup_failure_is_typed_and_secret_absent(
    isolated_memory_db, harness, monkeypatch
):
    secret = "super_secret_lookup_zz9"
    _seed_terminal("claude_code", native_session_id=SESSION_ID)
    _seed_roster("claude_code", native_session_id=SESSION_ID)
    _seed_exact_attached_attachment(
        "claude_code", SESSION_ID, terminal_id=TERMINAL_ID, generation=GENERATION
    )

    def _exploding_get(*args, **kwargs):
        raise native_attachment.NativeAttachmentUnavailable(f"lookup {secret}")

    monkeypatch.setattr(native_attachment, "get", _exploding_get)
    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "attachment-unavailable"
    assert secret not in str(outcome)
    assert harness.typed == []


# ---------------------------------------------------------------------------
# Follow-up 4: fixes 1-4
# ---------------------------------------------------------------------------


def _binding_native_id() -> Optional[str]:
    with database.SessionLocal() as db:
        row = (
            db.query(database.ManagedLaunchV2ReservationModel)
            .filter_by(terminal_id=TERMINAL_ID)
            .first()
        )
        if row is None or not row.binding_json:
            return None
        return json.loads(row.binding_json).get("native_session_id")


# --- 1: already-known requires BOTH repair targets ---


def test_terminal_binding_known_lineage_missing_repairs_the_lineage(isolated_memory_db, harness):
    # terminal=A + binding=A, lineage missing, exact attachment A: the
    # binding must NOT make the missing lineage look complete — the repair
    # verifies the panel and fills the lineage to A.
    _seed_terminal("claude_code", native_session_id=SESSION_ID)
    _seed_roster("claude_code")
    _seed_exact_attached_attachment(
        "claude_code", SESSION_ID, terminal_id=TERMINAL_ID, generation=GENERATION
    )
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())

    outcome = _claude_call()
    assert outcome["status"] == "repaired"
    assert outcome["task_bytes_submitted"] is False
    # The missing lineage is filled; the terminal and binding are unchanged.
    assert _current_lineage()["native_session_id"] == SESSION_ID
    assert _terminal_row().v2_native_session_id == SESSION_ID
    assert _binding_native_id() == SESSION_ID


def test_lineage_binding_known_terminal_missing_repairs_the_terminal(isolated_memory_db, harness):
    _seed_terminal("claude_code")
    _seed_roster("claude_code", native_session_id=SESSION_ID)
    _seed_exact_attached_attachment(
        "claude_code", SESSION_ID, terminal_id=TERMINAL_ID, generation=GENERATION
    )
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())

    outcome = _claude_call()
    assert outcome["status"] == "repaired"
    assert outcome["task_bytes_submitted"] is False
    assert _terminal_row().v2_native_session_id == SESSION_ID
    assert _current_lineage()["native_session_id"] == SESSION_ID
    assert _binding_native_id() == SESSION_ID


# --- 2: an ordinary exact attachment is not a partial status repair ---


def test_ordinary_attachment_reuse_without_a_status_receipt(isolated_memory_db, harness):
    # binding-only (both targets missing) + an ordinary exact attachment
    # (pre-launch chosen-session intent, no adoption receipt): the repair
    # verifies the panel and fills BOTH targets while preserving the
    # attachment owner and its original intent.
    _seed_terminal("claude_code", binding_session_id=SESSION_ID)
    _seed_roster("claude_code")
    _seed_exact_attached_attachment(
        "claude_code", SESSION_ID, terminal_id=TERMINAL_ID, generation=GENERATION
    )
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())

    outcome = _claude_call()
    assert outcome["status"] == "repaired"
    assert outcome["task_bytes_submitted"] is False
    assert _terminal_row().v2_native_session_id == SESSION_ID
    assert _current_lineage()["native_session_id"] == SESSION_ID
    # The ordinary attachment is preserved, intent and all.
    attachment = native_attachment.get("claude_code", SESSION_ID)
    assert attachment["state"] == native_attachment.ATTACHED
    assert attachment["owner"]["terminal_id"] == TERMINAL_ID
    assert (
        attachment["intent"]["acquisition_method"]
        == native_attachment.ACQUISITION_CHOSEN_SESSION_ID
    )
    assert attachment["adoption_receipt"] is None


def test_partial_target_ordinary_attachment_repairs_only_missing_target(
    isolated_memory_db, harness
):
    _seed_terminal("claude_code", native_session_id=SESSION_ID)
    _seed_roster("claude_code")
    _seed_exact_attached_attachment(
        "claude_code", SESSION_ID, terminal_id=TERMINAL_ID, generation=GENERATION
    )
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())

    outcome = _claude_call()
    assert outcome["status"] == "repaired"
    assert _current_lineage()["native_session_id"] == SESSION_ID
    assert _terminal_row().v2_native_session_id == SESSION_ID
    attachment = native_attachment.get("claude_code", SESSION_ID)
    assert (
        attachment["intent"]["acquisition_method"]
        == native_attachment.ACQUISITION_CHOSEN_SESSION_ID
    )


def test_status_discovered_intent_without_receipt_is_reconcile_zero_bytes(
    isolated_memory_db, harness
):
    # A record whose intent says provider_status_discovered but lacks the
    # required adoption receipt is incomplete/corrupt status repair.
    _seed_all("claude_code")
    identity = {"pid": PANE_PID, "start_marker": START_MARKER}
    with database.SessionLocal() as db:
        db.add(
            database.NativeSessionAttachmentModel(
                provider="claude_code",
                native_session_id=SESSION_ID,
                state=native_attachment.ATTACHED,
                owner_terminal_id=TERMINAL_ID,
                owner_generation=GENERATION,
                owner_execution_mode=em.NATIVE_TUI,
                owner_pane_id=PANE_ID,
                owner_process_identity_json=json.dumps(identity, sort_keys=True),
                intent_json=json.dumps(
                    {
                        "schema": native_attachment.INTENT_SCHEMA,
                        "acquisition_method": native_attachment.ACQUISITION_STATUS_DISCOVERED,
                        "acquisition_receipt": {},
                        "admits_only_new_instructions": True,
                        "replays_task_bytes": False,
                    }
                ),
                release_proof_json=None,
                adoption_receipt_json=None,
                ambiguity_reason=None,
                epoch=0,
                created_at="now",
                updated_at="now",
            )
        )
        db.commit()
    harness.screens.append(claude_panel_rows())
    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "attachment-reconcile"
    assert harness.typed == []
    assert _evidence_rows() == []


def test_malformed_non_null_adoption_receipt_remains_zero_byte_reconcile(
    isolated_memory_db, harness
):
    op = _partial_exact_owner(harness, lambda r: r.__setitem__("schema", "wrong"))
    outcome = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        provider_version=CLAUDE_VERSION,
        operation_id=op,
    )
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "attachment-reconcile"
    assert harness.typed == []
    assert _evidence_rows() == []


def test_ordinary_attachment_panel_mismatch_never_creates_a_second_owner(
    isolated_memory_db, harness
):
    # The panel attests B while the exact ordinary owner holds A: a second
    # owner is never created, and nothing is mutated.
    _seed_terminal("claude_code", binding_session_id=SESSION_ID)
    _seed_roster("claude_code")
    _seed_exact_attached_attachment(
        "claude_code", SESSION_ID, terminal_id=TERMINAL_ID, generation=GENERATION
    )
    harness.screens.append(claude_panel_rows(session_id="11111111-2222-4333-8444-555555555555"))
    harness.styled_screens.append(claude_composer_rows())

    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "identity-conflict"
    assert _terminal_row().v2_native_session_id is None
    assert native_attachment.get("claude_code", "11111111-2222-4333-8444-555555555555") is None


# --- 3: reservation execution_mode exactness ---


def test_acp_reservation_with_native_binding_fails_closed_before_pane_io(
    isolated_memory_db, harness
):
    _seed_terminal("claude_code", binding=_default_binding("claude_code", SESSION_ID))
    _seed_roster("claude_code")
    with database.SessionLocal() as db:
        row = (
            db.query(database.ManagedLaunchV2ReservationModel)
            .filter_by(terminal_id=TERMINAL_ID)
            .one()
        )
        row.execution_mode = "acp"
        db.commit()
    harness.screens.append(claude_panel_rows())
    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "binding-unavailable"
    assert harness.typed == []
    assert _terminal_row().v2_native_session_id is None


# --- 4: evidence terminal_id exactness ---


def test_evidence_terminal_id_mismatch_is_operation_conflict(isolated_memory_db, harness):
    _seed_all("claude_code")
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())
    op = _uuid()

    first = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        provider_version=CLAUDE_VERSION,
        operation_id=op,
    )
    assert first["status"] == "repaired"
    harness.typed.clear()

    # Corrupt the stored evidence's terminal id.
    with database.SessionLocal() as db:
        row = db.query(database.NativeStatusRepairEvidenceModel).filter_by(operation_id=op).one()
        row.terminal_id = "d4e5f607"
        db.commit()

    second = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        provider_version=CLAUDE_VERSION,
        operation_id=op,
    )
    assert second["status"] == "refused"
    assert second["reason"] == "operation-conflict"
    assert harness.typed == []
    assert "d4e5f607" not in str(second)
    assert len(_evidence_rows()) == 1


# ---------------------------------------------------------------------------
# Follow-up 5: ordinary-attachment exact boundary
# ---------------------------------------------------------------------------


def _insert_exact_attachment(
    provider: str,
    session_id: str,
    *,
    intent: dict,
    receipt: Optional[dict] = None,
    mode: str = em.NATIVE_TUI,
    terminal_id: str = TERMINAL_ID,
    generation: str = GENERATION,
) -> None:
    """Directly insert an attachment whose owner matches the exact current
    facts, with a caller-supplied intent and optional receipt."""
    identity = {"pid": PANE_PID, "start_marker": START_MARKER}
    with database.SessionLocal() as db:
        db.add(
            database.NativeSessionAttachmentModel(
                provider=provider,
                native_session_id=session_id,
                state=native_attachment.ATTACHED,
                owner_terminal_id=terminal_id,
                owner_generation=generation,
                owner_execution_mode=mode,
                owner_pane_id=PANE_ID,
                owner_process_identity_json=json.dumps(identity, sort_keys=True),
                intent_json=json.dumps(intent, sort_keys=True),
                release_proof_json=None,
                adoption_receipt_json=json.dumps(receipt, sort_keys=True) if receipt else None,
                ambiguity_reason=None,
                epoch=0,
                created_at="now",
                updated_at="now",
            )
        )
        db.commit()


def _sanctioned_intent(method: str = native_attachment.ACQUISITION_CHOSEN_SESSION_ID) -> dict:
    return native_attachment.acquire_intent(
        acquisition_method=method,
        acquisition_receipt={"schema": "test-intent"},
        admits_only_new_instructions=True,
        replays_task_bytes=False,
    )


def _assert_zero_byte_reconcile(harness, outcome):
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "attachment-reconcile"
    assert harness.typed == []
    assert _terminal_row().v2_native_session_id is None
    assert _evidence_rows() == []


def test_non_mapping_present_adoption_receipt_reconciles_before_bytes(isolated_memory_db, harness):
    _seed_terminal("claude_code", binding_session_id=SESSION_ID)
    _seed_roster("claude_code")
    with database.SessionLocal() as db:
        identity = {"pid": PANE_PID, "start_marker": START_MARKER}
        db.add(
            database.NativeSessionAttachmentModel(
                provider="claude_code",
                native_session_id=SESSION_ID,
                state=native_attachment.ATTACHED,
                owner_terminal_id=TERMINAL_ID,
                owner_generation=GENERATION,
                owner_execution_mode=em.NATIVE_TUI,
                owner_pane_id=PANE_ID,
                owner_process_identity_json=json.dumps(identity, sort_keys=True),
                intent_json=json.dumps(_sanctioned_intent(), sort_keys=True),
                release_proof_json=None,
                adoption_receipt_json='"corrupt-receipt"',
                ambiguity_reason=None,
                epoch=0,
                created_at="now",
                updated_at="now",
            )
        )
        db.commit()
    harness.screens.append(claude_panel_rows())
    outcome = _claude_call()
    _assert_zero_byte_reconcile(harness, outcome)


def test_missing_or_unknown_acquisition_intent_reconciles_before_bytes(isolated_memory_db, harness):
    _seed_terminal("claude_code", binding_session_id=SESSION_ID)
    _seed_roster("claude_code")
    # Missing intent entirely.
    _insert_exact_attachment("claude_code", SESSION_ID, intent={})
    harness.screens.append(claude_panel_rows())
    outcome = _claude_call()
    _assert_zero_byte_reconcile(harness, outcome)

    # Unknown future acquisition method in a schema-correct intent.
    other_id, other_gen = "d4e5f607", _uuid()
    _seed_terminal(
        "claude_code", terminal_id=other_id, generation=other_gen, binding_session_id=SESSION_ID
    )
    _seed_roster("claude_code", terminal_id=other_id, generation=other_gen)
    _insert_exact_attachment(
        "claude_code",
        "22222222-2222-4222-8222-222222222222",
        intent={
            "schema": native_attachment.INTENT_SCHEMA,
            "acquisition_method": "unknown-future-method",
            "acquisition_receipt": {"schema": "x"},
            "admits_only_new_instructions": True,
            "replays_task_bytes": False,
        },
        terminal_id=other_id,
        generation=other_gen,
    )
    harness.screens.append(claude_panel_rows())
    outcome = nsr.repair_terminal_native_identity(
        terminal_id=other_id,
        generation=other_gen,
        provider_version=CLAUDE_VERSION,
        operation_id=_uuid(),
    )
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "attachment-reconcile"
    assert harness.typed == []


def test_broken_core_intent_assertion_reconciles_before_bytes(isolated_memory_db, harness):
    _seed_terminal("claude_code", binding_session_id=SESSION_ID)
    _seed_roster("claude_code")
    intent = _sanctioned_intent()
    intent["replays_task_bytes"] = True
    _insert_exact_attachment("claude_code", SESSION_ID, intent=intent)
    harness.screens.append(claude_panel_rows())
    outcome = _claude_call()
    _assert_zero_byte_reconcile(harness, outcome)


def test_acp_exact_owner_reconciles_before_bytes(isolated_memory_db, harness):
    _seed_terminal("claude_code", binding_session_id=SESSION_ID)
    _seed_roster("claude_code")
    _insert_exact_attachment("claude_code", SESSION_ID, intent=_sanctioned_intent(), mode=em.ACP)
    harness.screens.append(claude_panel_rows())
    outcome = _claude_call()
    _assert_zero_byte_reconcile(harness, outcome)


def test_invalid_ordinary_attachment_session_id_reconciles_before_bytes(
    isolated_memory_db, harness
):
    _seed_terminal("claude_code", binding_session_id=SESSION_ID)
    _seed_roster("claude_code")
    _insert_exact_attachment("claude_code", "not-a-canonical-session", intent=_sanctioned_intent())
    harness.screens.append(claude_panel_rows())
    outcome = _claude_call()
    _assert_zero_byte_reconcile(harness, outcome)


def test_sanctioned_ordinary_owner_stays_byte_identical(isolated_memory_db, harness):
    # The sanctioned ordinary exact owner remains green, and its original
    # intent and receipt-null state stay byte-identical after the repair.
    _seed_terminal("claude_code", binding_session_id=SESSION_ID)
    _seed_roster("claude_code")
    intent = _sanctioned_intent()
    _insert_exact_attachment("claude_code", SESSION_ID, intent=intent)
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())

    outcome = _claude_call()
    assert outcome["status"] == "repaired"
    assert outcome["task_bytes_submitted"] is False
    attachment = native_attachment.get("claude_code", SESSION_ID)
    assert attachment["intent"] == intent
    assert attachment["adoption_receipt"] is None
    assert _terminal_row().v2_native_session_id == SESSION_ID
    assert _current_lineage()["native_session_id"] == SESSION_ID


# ---------------------------------------------------------------------------
# Follow-up 6: raw receipt presence/readability boundary
# ---------------------------------------------------------------------------


def _insert_exact_with_raw_receipt(raw: Optional[str]):
    identity = {"pid": PANE_PID, "start_marker": START_MARKER}
    with database.SessionLocal() as db:
        db.add(
            database.NativeSessionAttachmentModel(
                provider="claude_code",
                native_session_id=SESSION_ID,
                state=native_attachment.ATTACHED,
                owner_terminal_id=TERMINAL_ID,
                owner_generation=GENERATION,
                owner_execution_mode=em.NATIVE_TUI,
                owner_pane_id=PANE_ID,
                owner_process_identity_json=json.dumps(identity, sort_keys=True),
                intent_json=json.dumps(_sanctioned_intent(), sort_keys=True),
                release_proof_json=None,
                adoption_receipt_json=raw,
                ambiguity_reason=None,
                epoch=0,
                created_at="now",
                updated_at="now",
            )
        )
        db.commit()


def test_raw_invalid_json_receipt_is_present_and_reconciles_before_bytes(
    isolated_memory_db, harness
):
    _seed_terminal("claude_code", binding_session_id=SESSION_ID)
    _seed_roster("claude_code")
    _insert_exact_with_raw_receipt("{not-json")
    harness.screens.append(claude_panel_rows())
    outcome = _claude_call()
    _assert_zero_byte_reconcile(harness, outcome)


def test_json_null_receipt_is_present_and_reconciles_before_bytes(isolated_memory_db, harness):
    _seed_terminal("claude_code", binding_session_id=SESSION_ID)
    _seed_roster("claude_code")
    _insert_exact_with_raw_receipt("null")
    harness.screens.append(claude_panel_rows())
    outcome = _claude_call()
    _assert_zero_byte_reconcile(harness, outcome)


def test_list_parsed_receipt_reconciles_before_bytes(isolated_memory_db, harness):
    _seed_terminal("claude_code", binding_session_id=SESSION_ID)
    _seed_roster("claude_code")
    _insert_exact_with_raw_receipt("[1, 2, 3]")
    harness.screens.append(claude_panel_rows())
    outcome = _claude_call()
    _assert_zero_byte_reconcile(harness, outcome)


def test_absent_sql_receipt_is_ordinary_and_green(isolated_memory_db, harness):
    _seed_terminal("claude_code", binding_session_id=SESSION_ID)
    _seed_roster("claude_code")
    _insert_exact_attachment("claude_code", SESSION_ID, intent=_sanctioned_intent())
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())

    outcome = _claude_call()
    assert outcome["status"] == "repaired"
    attachment = native_attachment.get("claude_code", SESSION_ID)
    assert attachment["adoption_receipt_present"] is False
    assert attachment["adoption_receipt"] is None
    assert _terminal_row().v2_native_session_id == SESSION_ID
    assert _current_lineage()["native_session_id"] == SESSION_ID


def test_projection_exposes_only_bounded_presence_facts_never_raw_bytes(
    isolated_memory_db, harness
):
    _seed_terminal("claude_code", binding_session_id=SESSION_ID)
    _seed_roster("claude_code")
    _insert_exact_with_raw_receipt("{not-json")
    record = native_attachment.get("claude_code", SESSION_ID)
    assert record["adoption_receipt"] is None
    assert record["adoption_receipt_present"] is True
    assert record["adoption_receipt_readable"] is False
    assert "{not-json" not in str(record)


# ---------------------------------------------------------------------------
# PR #99 curated review fixes
# ---------------------------------------------------------------------------

# --- i-0001: operation-conflict must not disclose stored evidence ---


def test_operation_conflict_does_not_disclose_stored_evidence(isolated_memory_db, harness):
    _seed_all("claude_code")
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())
    op = _uuid()

    first = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        provider_version=CLAUDE_VERSION,
        operation_id=op,
    )
    assert first["status"] == "repaired"

    # Reuse the same operation id against a DIFFERENT valid terminal.
    other_id, other_gen = "e5f60708", _uuid()
    _seed_terminal("claude_code", terminal_id=other_id, generation=other_gen)
    _seed_roster("claude_code", terminal_id=other_id, generation=other_gen)
    second = nsr.repair_terminal_native_identity(
        terminal_id=other_id,
        generation=other_gen,
        provider_version=CLAUDE_VERSION,
        operation_id=op,
    )
    assert second["status"] == "refused"
    assert second["reason"] == "operation-conflict"
    # No stored evidence from the completed operation leaks into the body.
    assert second["native_session_id"] is None
    assert second["evidence_sha256"] is None
    assert second["parser_key"] is None
    assert second["provider_version"] is None
    assert SESSION_ID not in str(second)
    assert first["evidence_sha256"] not in str(second)


# --- i-0006: live pane/server/process refusal branches ---


@pytest.mark.parametrize(
    "seam_mutate, expected_reason",
    [
        pytest.param(
            lambda s: setattr(s, "pane_identity_error", RuntimeError("tmux gone")),
            "pane-identity-drift",
            id="pane-unobservable",
        ),
        pytest.param(
            lambda s: setattr(
                s,
                "pane_identity",
                PaneControlIdentity(
                    pane_id=PANE_ID,
                    window_id=WINDOW_ID,
                    session_id=TMUX_SESSION_ID,
                    pane_pid=9999,
                    session_name=SESSION_NAME,
                    window_name=f"w-{TERMINAL_ID}",
                    bracketed_paste_proven=False,
                    dead=False,
                    server_socket_path=SERVER_SOCKET,
                ),
            ),
            "pane-identity-drift",
            id="pane-recycled",
        ),
        pytest.param(
            lambda s: setattr(s, "server_identity", None),
            "server-identity-drift",
            id="server-unobservable",
        ),
        pytest.param(
            lambda s: setattr(s, "server_identity", "/tmp/other.sock"),
            "server-identity-drift",
            id="server-drift",
        ),
        pytest.param(
            lambda s: setattr(s, "live_start_marker", None),
            "process-identity-unobservable",
            id="marker-unreadable",
        ),
        pytest.param(
            lambda s: setattr(s, "live_start_marker", "Mon Jan 1 00:00:00 2024"),
            "process-identity-drift",
            id="marker-drift",
        ),
    ],
)
def test_live_observation_refusals_are_directly_protected(
    isolated_memory_db, harness, seam_mutate, expected_reason
):
    # Stored row/roster facts stay valid; only the LIVE observation drifts.
    _seed_all("claude_code")
    seam_mutate(harness)
    harness.screens.append(claude_panel_rows())
    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == expected_reason
    assert harness.typed == []
    assert _terminal_row().v2_native_session_id is None
    assert _evidence_rows() == []


# --- i-0013: final live-occurrence fence before adoption/commit ---


def test_pane_process_drift_after_observation_before_adoption_refuses(
    isolated_memory_db, harness, monkeypatch
):
    _seed_all("claude_code")
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())
    real_verdict = nsr._capture_panel_verdict

    def _mutating_verdict(provider, pane_id, plan, **kwargs):
        verdict = real_verdict(provider, pane_id, plan, **kwargs)
        harness.live_start_marker = "Mon Jan 1 00:00:00 2024"
        return verdict

    monkeypatch.setattr(nsr, "_capture_panel_verdict", _mutating_verdict)
    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "process-identity-drift"
    assert _terminal_row().v2_native_session_id is None
    assert _current_lineage()["native_session_id"] is None
    assert _evidence_rows() == []
    assert native_attachment.get("claude_code", SESSION_ID) is None


# --- i-0018: contradictory Kimi panel refused ---


def test_kimi_mixed_live_session_and_none_panel_is_refused():
    with pytest.raises(nsr.PanelParseError):
        nsr.parse_kimi_status(kimi_panel_rows() + ["Session none"])


def test_kimi_mixed_panel_operation_refuses_without_mutation(isolated_memory_db, harness):
    _seed_legacy("kimi_cli")
    _seed_roster("kimi_cli", generation=None)
    harness.screens.append(kimi_panel_rows() + ["Session none"])
    outcome = _call(
        provider_version=KIMI_VERSION,
        generation=None,
        physical_occurrence=CALLBACK_TARGET,
    )
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "panel-unparsed"
    assert _legacy_row().native_session_id is None
    assert _evidence_rows() == []


# --- i-0019: Kimi Session none + exact ordinary owner -> conflict ---


def test_kimi_session_none_with_exact_ordinary_owner_is_a_conflict(isolated_memory_db, harness):
    _seed_legacy("kimi_cli")
    _seed_roster("kimi_cli", generation=None)
    _insert_exact_attachment(
        "kimi_cli", KIMI_SESSION_ID, intent=_sanctioned_intent(), generation=CALLBACK_TARGET
    )
    harness.screens.append(kimi_panel_rows(session_id=None))
    outcome = _call(
        provider_version=KIMI_VERSION,
        generation=None,
        physical_occurrence=CALLBACK_TARGET,
    )
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "identity-conflict"
    assert _legacy_row().native_session_id is None
    # The exact ordinary owner is untouched.
    attachment = native_attachment.get("kimi_cli", KIMI_SESSION_ID)
    assert attachment["state"] == native_attachment.ATTACHED
    assert _evidence_rows() == []


# --- i-0020: binding drift in the final commit window ---


def test_binding_drift_in_the_final_commit_window_refuses(isolated_memory_db, harness, monkeypatch):
    _seed_all("claude_code")
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())
    real_adopt = nsr._adopt_running_owner

    def _rewrite_binding(**kwargs):
        with database.SessionLocal() as db:
            row = (
                db.query(database.ManagedLaunchV2ReservationModel)
                .filter_by(terminal_id=TERMINAL_ID)
                .one()
            )
            row.binding_json = json.dumps(
                _default_binding("claude_code", "99999999-8888-4777-8666-5555555555aa")
            )
            db.commit()
        return real_adopt(**kwargs)

    monkeypatch.setattr(nsr, "_adopt_running_owner", _rewrite_binding)
    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "binding-drift"
    assert _terminal_row().v2_native_session_id is None
    assert _current_lineage()["native_session_id"] is None
    assert _evidence_rows() == []


# ---------------------------------------------------------------------------
# cond-0427: a submitted status action is observed, never inferred from tmux
# ---------------------------------------------------------------------------


def _attempt(operation_id: str) -> dict[str, Any]:
    journal = nsr.repair_observation_attempt(operation_id)
    assert journal is not None
    return journal


@pytest.fixture
def fast_barrier(monkeypatch):
    """Shrink the codex barrier's bounds without shortening its windows.

    The sandbox pins the pane-ready timeout to 0.4s, which is shorter than
    the real 3s/5s barrier bounds and would cut every observation window
    short — turning a positive ``unsubmitted`` sighting into ``unknown``
    and letting these tests pass for the wrong reason.  Here the barrier is
    made small and the deadline large, so each window runs its full bound
    and the classification under test is the real one.
    """
    monkeypatch.setattr(v2, "NATIVE_PANE_READY_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setitem(
        npi._SUBMISSION_BARRIERS,
        "codex",
        npi.SubmissionBarrier(
            compose_settle_seconds=0.2,
            post_enter_seconds=0.2,
            poll_interval_seconds=0.02,
            composer_tail_rows=4,
        ),
    )


def test_composer_keeping_status_refuses_submission_unproven(
    isolated_memory_db, harness, fast_barrier
):
    """A pane that takes the bytes and never submits them is refused.

    tmux exits 0 for both a submitted and an unsubmitted Enter, so the
    barrier's positive ``unsubmitted`` sighting is the only thing that can
    tell them apart.  Exactly one Enter is sent and no second is attempted.
    """
    harness.screens.append(codex_panel_rows())
    harness.composer_keeps_text = True
    _seed_all("codex")

    op = _uuid()
    outcome = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        provider_version=CODEX_VERSION,
        operation_id=op,
    )

    assert outcome["status"] == nsr.STATUS_REFUSED
    assert outcome["reason"] == "submission-unproven"
    # Not ``panel-unparsed``: the panel was never the problem, and pointing a
    # diagnosing operator at the parser would be a wrong answer.
    #
    # The window ran its full bound and ended on the POSITIVE sighting that
    # the composer still held the text.  Asserting the classification keeps
    # this test from passing on a deadline-cut ``unknown``, which would be a
    # different fact reaching the same refusal.
    assert "(unsubmitted)" in outcome["detail"]
    assert _typed_bytes(harness) == [("literal", "/status"), ("enter", "")]

    journal = _attempt(op)
    assert journal["status"] == nsr.OBSERVATION_ATTEMPTED
    assert journal["status_action_count"] == 0

    assert _terminal_row().v2_native_session_id is None
    assert _current_lineage()["native_session_id"] is None
    assert _evidence_rows() == []


def test_text_never_compose_visible_withholds_the_enter(isolated_memory_db, harness, fast_barrier):
    """When the text never reaches the composer, the Enter is withheld.

    Zero Enters is the point: submitting into a composer that was never
    proven to hold ``/status`` is how a stray Enter becomes somebody's
    prompt.  The composer is deliberately not cleared afterwards.
    """
    harness.screens.append(codex_panel_rows())
    harness.composer_drops_literal = True
    _seed_all("codex")

    op = _uuid()
    outcome = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        provider_version=CODEX_VERSION,
        operation_id=op,
    )

    assert outcome["status"] == nsr.STATUS_REFUSED
    assert outcome["reason"] == "submission-unproven"
    assert _typed_bytes(harness) == [("literal", "/status")]
    assert not any(entry["kind"] == "enter" for entry in harness.typed)

    journal = _attempt(op)
    assert journal["status"] == nsr.OBSERVATION_ATTEMPTED
    assert journal["status_action_count"] == 0
    assert _terminal_row().v2_native_session_id is None
    assert _evidence_rows() == []


def test_unproven_submission_is_not_retried_in_place(isolated_memory_db, harness, fast_barrier):
    """An exact retry after an unproven submission never resends /status."""
    harness.screens.append(codex_panel_rows())
    harness.composer_keeps_text = True
    _seed_all("codex")

    op = _uuid()
    first = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        provider_version=CODEX_VERSION,
        operation_id=op,
    )
    assert first["reason"] == "submission-unproven"
    sent_before = _typed_bytes(harness)

    second = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        provider_version=CODEX_VERSION,
        operation_id=op,
    )
    assert second["status"] == nsr.STATUS_REFUSED
    assert second["reason"] == "observation-attempt-ambiguous"
    assert _typed_bytes(harness) == sent_before


def test_observed_submission_records_the_action(isolated_memory_db, harness):
    """A composer that gives the text up is what records a submitted action."""
    harness.screens.append(codex_panel_rows())
    _seed_all("codex")

    op = _uuid()
    outcome = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        provider_version=CODEX_VERSION,
        operation_id=op,
    )

    assert outcome["status"] == "repaired"
    journal = _attempt(op)
    assert journal["status"] == nsr.OBSERVATION_OBSERVED
    assert journal["status_action_count"] == 1


def test_provider_without_a_pinned_barrier_keeps_its_existing_behaviour(
    isolated_memory_db, harness
):
    """Claude has no pinned composer, so no submission observation is run.

    ``submission_barrier_for`` returning None means "not proven", never
    "guess one": the legacy fused write stands and claims nothing extra.
    """
    assert npi.submission_barrier_for("claude_code") is None
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())
    # The composer keeps the text; without a barrier nothing observes it,
    # and the repair proceeds exactly as it did before cond-0427.
    harness.composer_keeps_text = True
    _seed_all("claude_code")

    outcome = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        provider_version=CLAUDE_VERSION,
        operation_id=_uuid(),
    )

    assert outcome["status"] == "repaired"
    assert outcome["native_session_id"] == SESSION_ID


def test_deadline_cut_observation_is_unknown_not_unsubmitted(isolated_memory_db, harness):
    """A window cut by the deadline classifies ``unknown``, never ``unsubmitted``.

    Deliberately runs WITHOUT the ``fast_barrier`` fixture: the sandbox pins
    the pane-ready timeout well below the barrier's own bound, so the
    post-Enter window is cut short and nothing about what the composer did
    can be classified.  Both classifications refuse identically, which is
    exactly why the distinction needs its own test -- collapsing them would
    turn "we could not look" into the positive sighting "it did not submit",
    and no assertion on the refusal reason alone would notice.
    """
    harness.screens.append(codex_panel_rows())
    harness.composer_keeps_text = True
    _seed_all("codex")

    op = _uuid()
    outcome = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID,
        generation=GENERATION,
        provider_version=CODEX_VERSION,
        operation_id=op,
    )

    assert outcome["status"] == nsr.STATUS_REFUSED
    assert outcome["reason"] == "submission-unproven"
    assert "(unknown)" in outcome["detail"]
    assert "(unsubmitted)" not in outcome["detail"]

    journal = _attempt(op)
    assert journal["status"] == nsr.OBSERVATION_ATTEMPTED
    assert journal["status_action_count"] == 0
