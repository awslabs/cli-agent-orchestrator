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
import re
import threading
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
        return list(self.screens[-1])

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

    def typed_enter(self) -> None:
        self.typed.append({"kind": "enter"})

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


def _seed_all(provider: str) -> dict[str, Any]:
    _seed_terminal(provider)
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
    _seed_all(provider)

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
    harness.screens.append(kimi_panel_rows(session_id=None))
    _seed_all("kimi_cli")

    outcome = _call(provider_version=KIMI_VERSION, generation=GENERATION)
    assert outcome["status"] == "identity-still-missing"
    assert outcome["native_session_id"] is None
    assert outcome["evidence_sha256"] is None
    assert outcome["provider_version"] == KIMI_VERSION
    assert _typed_bytes(harness) == [("literal", "/status"), ("enter", "")]
    assert _terminal_row().v2_native_session_id is None
    assert _current_lineage()["native_session_id"] is None
    assert native_attachment.get("kimi_cli", "anything") is None
    assert _evidence_rows() == []


# ---------------------------------------------------------------------------
# A: legacy/generationless identity
# ---------------------------------------------------------------------------


def test_legacy_happy_path_binds_the_callback_target_occurrence(isolated_memory_db, harness):
    _seed_legacy_all("claude_code")
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())

    outcome = _call(generation=None)
    assert outcome["status"] == "repaired"
    assert outcome["generation"] == CALLBACK_TARGET
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


def test_claude_escape_exactly_once_on_identity_conflict(isolated_memory_db, harness):
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
    _assert_escape_contract(
        harness, outcome, expected_status="refused", expected_reason="identity-conflict"
    )
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
    _seed_all("claude_code")
    outcome = _claude_call(provider_version="9.9.9")
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


def test_stored_same_id_with_attachment_is_already_known_zero_bytes(isolated_memory_db, harness):
    # Both sides know the same id and the attachment exists: a typed no-op
    # with zero /status, zero evidence, zero mutation.
    _seed_terminal("claude_code", native_session_id=SESSION_ID)
    _seed_roster("claude_code", native_session_id=SESSION_ID)
    _declare_attachment("claude_code", SESSION_ID, terminal_id=TERMINAL_ID, generation=GENERATION)

    outcome = _claude_call()
    assert outcome["status"] == "already-known"
    assert outcome["native_session_id"] == SESSION_ID
    assert harness.typed == []
    assert _terminal_row().v2_native_session_id == SESSION_ID
    assert _evidence_rows() == []


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
    _declare_attachment("claude_code", SESSION_ID, terminal_id=TERMINAL_ID, generation=GENERATION)

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
    _seed_terminal("claude_code", native_session_id=SESSION_ID)
    _seed_roster("claude_code")
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())

    outcome = _claude_call()
    assert outcome["status"] == "repaired"
    assert outcome["native_session_id"] == SESSION_ID


def test_terminal_only_known_mismatch_is_a_typed_refusal_with_durable_unchanged(
    isolated_memory_db, harness
):
    _seed_terminal("claude_code", native_session_id=SESSION_ID)
    _seed_roster("claude_code")
    harness.screens.append(claude_panel_rows(session_id="11111111-2222-4333-8444-555555555555"))
    harness.styled_screens.append(claude_composer_rows())

    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "identity-conflict"
    assert _terminal_row().v2_native_session_id == SESSION_ID
    assert _current_lineage()["native_session_id"] is None
    assert _evidence_rows() == []


def test_lineage_only_known_must_match_the_panel(isolated_memory_db, harness):
    _seed_terminal("claude_code")
    _seed_roster("claude_code", native_session_id=SESSION_ID)
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())

    outcome = _claude_call()
    assert outcome["status"] == "repaired"
    assert outcome["native_session_id"] == SESSION_ID


def test_kimi_still_missing_cannot_silently_ignore_a_known_id(isolated_memory_db, harness):
    # A known id exists but the Kimi panel renders no session: the known id
    # could not be verified, so it is a typed refusal with durable unchanged.
    _seed_terminal("kimi_cli", native_session_id=KIMI_SESSION_ID)
    _seed_roster("kimi_cli")
    harness.screens.append(kimi_panel_rows(session_id=None))

    outcome = _call(provider_version=KIMI_VERSION, generation=GENERATION)
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "identity-conflict"
    assert _terminal_row().v2_native_session_id == KIMI_SESSION_ID
    assert _evidence_rows() == []


def test_lineage_only_known_mismatch_is_a_typed_refusal_with_durable_unchanged(
    isolated_memory_db, harness
):
    _seed_terminal("claude_code")
    _seed_roster("claude_code", native_session_id=SESSION_ID)
    harness.screens.append(claude_panel_rows(session_id="11111111-2222-4333-8444-555555555555"))
    harness.styled_screens.append(claude_composer_rows())

    outcome = _claude_call()
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "identity-conflict"
    assert _terminal_row().v2_native_session_id is None
    assert _current_lineage()["native_session_id"] == SESSION_ID
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
    assert second["status"] == "refused"
    assert second["reason"] == "operation-conflict"
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
