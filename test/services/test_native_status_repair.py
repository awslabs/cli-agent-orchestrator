"""Native /status identity repair (cond-0377C), end to end over fakes.

The repair is the bounded M3-A health operation that recovers a missing
native session id on a *live, rostered* terminal: it holds the exact
terminal-generation lifecycle claim and the per-pane input lease across
one literal ``/status`` + one Enter, parses only provider/build-specific
identity fields, sends exactly one Escape for the Claude modal (in a
``finally`` that preserves the primary failure), proves the composer
restored, adopts an exclusive ``NativeSessionAttachmentModel`` owner for
the exact running pane/process, and commits the terminal row, the roster
lineage, and the bounded evidence digest in one transaction.

Everything this suite asserts is pinned to the fixtures the task records:

* Claude Code 2.1.226 — modal ``Session ID: <uuid>`` panel (the 2026-08-10
  canary), one Escape restores the composer.
* Codex 0.147.0 — ``Session: <uuid>``.
* Kimi Code 0.34.0 — ``Session session_<uuid>`` when one exists; a fresh
  untouched TUI may render no session row at all (typed
  ``identity-still-missing``, never a fabricated id).
* Muse Code 0.1.0 — ``Session: <uuid>`` (the existing strict panel parse,
  WITHOUT the launch's pre-task zero-turn requirement).

The suite covers the eleven required oracles: happy path per provider,
Kimi no-id, Escape-once across every failure class, lease contention,
drift refusals, idempotent replay, parser negatives, attachment
conflicts/adoption, transaction rollback, the endpoint contract, and the
never-blocks-teardown / no-task-bytes guarantees.
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

#: The canary's exact session id (Claude 2.1.226 fixture), reused across
#: providers since all four render canonical UUIDs.
SESSION_ID = "4f5f46c7-b660-4f6f-a144-d2c6dceccf95"
KIMI_SESSION_ID = f"session_{SESSION_ID}"

TERMINAL_ID = "a1b2c3d4"
GENERATION = "00000000-0000-4000-8000-000000000001"
PANE_ID = "%7"
WINDOW_ID = "@7"
TMUX_SESSION_ID = "$1"
SERVER_SOCKET = "/private/tmp/cao-native.sock"
PANE_PID = 4242
START_MARKER = "Thu Jul 24 10:00:00 2026"
SESSION_NAME = "cao-campaign"


def _uuid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Panel fixtures
# ---------------------------------------------------------------------------


def claude_panel_rows(
    session_id: str = SESSION_ID,
    *,
    version: str = CLAUDE_VERSION,
    drop: tuple[str, ...] = (),
    duplicates: tuple[str, ...] = (),
    header: str = "Settings  Status   Config   Usage   Stats",
) -> list[str]:
    """The sanitized canary /status modal, exactly as captured (with the
    literal ``[1m]`` styling fragments the plain capture retained)."""
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


def codex_panel_rows(session_id: str = SESSION_ID, *, extra: tuple[str, ...] = ()) -> list[str]:
    rows = [
        f"Session: {session_id}",
        "Model: gpt-5.4-codex",
        "cwd: /Users/x/repo",
    ]
    rows.extend(extra)
    return rows


def kimi_panel_rows(
    session_id: Optional[str] = KIMI_SESSION_ID, *, extra: tuple[str, ...] = ()
) -> list[str]:
    rows = ["Model: kimi-k2"]
    if session_id is not None:
        rows.append(f"Session {session_id}")
    else:
        # A fresh untouched Kimi TUI renders a Session label with no id
        # yet (the task fixture: no session row before the first
        # session-creating action).
        rows.append("Session -")
    rows.extend(extra)
    return rows


def muse_panel_rows(
    session_id: str = SESSION_ID,
    *,
    tokens: str = "120 tokens / 3 turns",
    run: str = "idle",
) -> list[str]:
    return [
        "╭────────────────────────────────────────────────────────────╮",
        "│ Session: " + session_id,
        "│ Model: muse-spark-1.2-contributor (reasoning high)",
        "│ Agent profile: native-basic",
        "│ Model provider: meta",
        "│ Directory: /Users/x/repo",
        f"│ Run: {run}",
        f"│ Token usage: {tokens}",
        "╰────────────────────────────────────────────────────────────╯",
        "⟩ ",
    ]


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _RepairHarness:
    """Every provider-facing boundary of a repair, recorded.

    ``typed`` records what was written into the pane (the /status literal,
    its one Enter, and the Claude Escape); ``screens`` serves the panel
    capture; ``styled_screens`` serves the post-Escape composer proof;
    ``turn_states`` drives the readiness observers.
    """

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
        # When set, the panel capture blocks until released (cancellation).
        self.block: Optional[threading.Event] = None
        # When set, the post-Escape composer proof never succeeds.
        self.composer_proof_rows: Optional[list[str]] = None
        self.calls: list[str] = []

    # --- seams the repair module reaches through ---

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
            self.block.wait(timeout=10)
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

    # --- the fake TmuxPaneInput ---

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


def _seed_roster(
    provider: str,
    *,
    terminal_id: str = TERMINAL_ID,
    generation: str = GENERATION,
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


def _current_lineage(
    terminal_id: str = TERMINAL_ID, generation: str = GENERATION
) -> dict[str, Any]:
    incarnation = roster.get_incarnation_by_terminal(terminal_id, generation=generation)
    agent = roster.get_agent(incarnation["agent_id"])
    return agent["current_lineage"]


def _evidence_rows() -> list[Any]:
    with database.SessionLocal() as db:
        return db.query(database.NativeStatusRepairEvidenceModel).all()


# ---------------------------------------------------------------------------
# Parser unit tests
# ---------------------------------------------------------------------------


class TestClaudeParser:
    def test_accepts_the_canary_panel_with_styling_fragments(self):
        parsed = nsr.parse_claude_status(claude_panel_rows(), pinned_version=CLAUDE_VERSION)
        assert parsed["session_id"] == SESSION_ID
        assert parsed["parser_key"] == "claude-modal-v1"

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

    def test_refuses_a_malformed_session_id(self):
        with pytest.raises(nsr.PanelParseError):
            nsr.parse_claude_status(
                claude_panel_rows(session_id="not-a-uuid"), pinned_version=CLAUDE_VERSION
            )

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
    def test_accepts_the_pinned_row(self):
        parsed = nsr.parse_codex_status(codex_panel_rows())
        assert parsed["session_id"] == SESSION_ID

    def test_refuses_duplicates(self):
        with pytest.raises(nsr.PanelParseError):
            nsr.parse_codex_status(
                codex_panel_rows(extra=("Session: 11111111-2222-4333-8444-555555555555",))
            )

    def test_refuses_missing_and_malformed(self):
        with pytest.raises(nsr.PanelParseError):
            nsr.parse_codex_status(["Model: gpt-5.4-codex"])
        with pytest.raises(nsr.PanelParseError):
            nsr.parse_codex_status(codex_panel_rows(session_id="session_xyz"))

    def test_refuses_a_claude_modal_capture(self):
        with pytest.raises(nsr.PanelParseError):
            nsr.parse_codex_status(claude_panel_rows())


class TestKimiParser:
    def test_accepts_a_session_row(self):
        parsed = nsr.parse_kimi_status(kimi_panel_rows())
        assert parsed["session_id"] == KIMI_SESSION_ID

    def test_no_session_row_is_a_typed_still_missing(self):
        parsed = nsr.parse_kimi_status(kimi_panel_rows(session_id=None))
        assert parsed["identity_still_missing"] is True
        assert "session_id" not in parsed

    def test_refuses_duplicate_session_rows(self):
        with pytest.raises(nsr.PanelParseError):
            nsr.parse_kimi_status(kimi_panel_rows(extra=(f"Session session_{_uuid()}",)))

    def test_refuses_a_malformed_session_id(self):
        with pytest.raises(nsr.PanelParseError):
            nsr.parse_kimi_status(kimi_panel_rows(session_id="session_not-a-uuid"))

    def test_refuses_a_claude_modal_capture_instead_of_inventing_still_missing(self):
        # Claude's "Session ID:"/"Session name:" rows must not be read as a
        # Kimi panel with no session: that would fabricate a still-missing
        # verdict out of another provider's rendering.
        with pytest.raises(nsr.PanelParseError):
            nsr.parse_kimi_status(claude_panel_rows())

    def test_refuses_garbage(self):
        with pytest.raises(nsr.PanelParseError):
            nsr.parse_kimi_status(["nothing here"])


class TestMuseParser:
    def test_accepts_a_post_work_panel_with_nonzero_turns(self):
        # The repair must NOT reuse the launch's pre-task zero-turn gate: a
        # legacy pane has worked; the panel still names the session.
        parsed = nsr.parse_muse_status(muse_panel_rows(tokens="120 tokens / 3 turns"))
        assert parsed["session_id"] == SESSION_ID

    def test_refuses_a_missing_session_row(self):
        with pytest.raises(nsr.PanelParseError):
            nsr.parse_muse_status(muse_panel_rows()[:2])

    def test_refuses_a_malformed_session_id(self):
        with pytest.raises(nsr.PanelParseError):
            nsr.parse_muse_status(muse_panel_rows(session_id="nope"))


class TestNormalization:
    def test_ansi_style_stripped_bounded_and_deterministic(self):
        styled = [
            "\x1b[1mSession ID:       " + SESSION_ID + "\x1b[0m",
            "  \x1b[2mModel: x\x1b[0m  ",
        ]
        plain = nsr.normalize_capture_rows(styled)
        assert plain == [f"Session ID:       {SESSION_ID}", "Model: x"]

    def test_evidence_digest_is_bounded_and_deterministic(self):
        rows = claude_panel_rows()
        first = nsr.evidence_digest(rows)
        assert re.fullmatch(r"[0-9a-f]{64}", first)
        assert nsr.evidence_digest(list(rows)) == first
        assert nsr.evidence_digest(["\x1b[1m" + row for row in rows]) == first
        huge = ["x" * 10000] * 3000
        digest = nsr.evidence_digest(huge)
        assert re.fullmatch(r"[0-9a-f]{64}", digest)


# ---------------------------------------------------------------------------
# Operation: happy paths
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
        terminal_id=TERMINAL_ID, generation=GENERATION, provider_version=version
    )

    assert outcome["status"] == "repaired"
    assert outcome["reason"] is None
    assert outcome["native_session_id"] == expected_id
    assert outcome["parser_key"] == parser_key
    assert outcome["provider"] == provider
    assert outcome["task_bytes_submitted"] is False
    assert re.fullmatch(r"[0-9a-f]{64}", outcome["evidence_sha256"])
    assert _typed_bytes(harness) == typed

    # The exact terminal row and the roster lineage now carry the id.
    assert _terminal_row().v2_native_session_id == expected_id
    lineage = _current_lineage()
    assert lineage["native_session_id"] == expected_id
    assert lineage["lineage_origin"] == roster.LINEAGE_ORIGIN_REPAIR
    assert lineage["acquisition_method"] == native_attachment.ACQUISITION_STATUS_DISCOVERED
    assert lineage["continuity_note"] and "status repair" in lineage["continuity_note"]

    # The exclusive attachment exists as attached with a status-repair
    # adoption receipt, and the evidence row is recorded.
    attachment = native_attachment.get(provider, expected_id)
    assert attachment is not None
    assert attachment["state"] == native_attachment.ATTACHED
    owner = attachment["owner"]
    assert owner["terminal_id"] == TERMINAL_ID
    assert owner["generation"] == GENERATION
    assert owner["pane_id"] == PANE_ID
    assert owner["process_identity"] == {"pid": PANE_PID, "start_marker": START_MARKER}
    receipt = attachment["adoption_receipt"]
    assert receipt is not None
    assert receipt["schema"] == native_attachment.STATUS_REPAIR_ADOPTION_SCHEMA
    assert receipt["evidence_sha256"] == outcome["evidence_sha256"]
    assert receipt["parser_key"] == parser_key
    assert receipt["provider_version"] == version
    assert receipt["pane_id"] == PANE_ID

    evidence = _evidence_rows()
    assert len(evidence) == 1
    assert evidence[0].native_session_id == expected_id
    assert evidence[0].evidence_sha256 == outcome["evidence_sha256"]
    assert evidence[0].terminal_id == TERMINAL_ID
    assert evidence[0].generation == GENERATION

    # Exactly one Escape, sent while the pane lease was still held, and a
    # successful post-Escape composer proof.
    if escape:
        assert harness.escapes == 1
        assert harness.lease_held_at_escape is True
        assert outcome["composer_restored"] is True
        assert "capture-styled" in harness.calls
    else:
        assert harness.escapes == 0


def test_kimi_no_id_is_typed_still_missing_with_zero_mutation(isolated_memory_db, harness):
    harness.screens.append(kimi_panel_rows(session_id=None))
    _seed_all("kimi_cli")

    outcome = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID, generation=GENERATION, provider_version=KIMI_VERSION
    )

    assert outcome["status"] == "identity-still-missing"
    assert outcome["native_session_id"] is None
    assert outcome["evidence_sha256"] is None
    # The panel was observed (one /status + one Enter), but nothing durable
    # was written and no id was fabricated.
    assert _typed_bytes(harness) == [("literal", "/status"), ("enter", "")]
    assert _terminal_row().v2_native_session_id is None
    assert _current_lineage()["native_session_id"] is None
    assert native_attachment.get("kimi_cli", "anything") is None
    assert _evidence_rows() == []


# ---------------------------------------------------------------------------
# Operation: Claude Escape exactly once across every failure class
# ---------------------------------------------------------------------------


def _seed_claude() -> None:
    _seed_all("claude_code")


def _assert_escape_contract(
    harness: _RepairHarness,
    outcome: Optional[dict[str, Any]],
    *,
    expected_status: Optional[str],
    expected_reason: Optional[str],
) -> None:
    # One /status, one Enter, exactly one Escape, sent under the lease.
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
    # The pane lease was released: the next acquisition is immediate.
    with pia.pane_input_lease(PANE_ID, holder="test", timeout=0.0):
        pass


def test_claude_escape_exactly_once_on_panel_timeout(isolated_memory_db, harness, monkeypatch):
    monkeypatch.setattr(v2, "NATIVE_PANE_READY_TIMEOUT_SECONDS", 0.15)
    _seed_claude()
    harness.screens.append(["garbage that never parses"])

    outcome = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID, generation=GENERATION, provider_version=CLAUDE_VERSION
    )
    _assert_escape_contract(
        harness, outcome, expected_status="refused", expected_reason="panel-unparsed"
    )
    assert _terminal_row().v2_native_session_id is None


def test_claude_escape_exactly_once_on_capture_exception(isolated_memory_db, harness, monkeypatch):
    monkeypatch.setattr(v2, "NATIVE_PANE_READY_TIMEOUT_SECONDS", 0.15)
    _seed_claude()
    harness.capture_errors.append(RuntimeError("capture exploded"))

    outcome = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID, generation=GENERATION, provider_version=CLAUDE_VERSION
    )
    _assert_escape_contract(
        harness, outcome, expected_status="refused", expected_reason="panel-unparsed"
    )
    assert _terminal_row().v2_native_session_id is None


def test_claude_escape_exactly_once_on_identity_conflict(isolated_memory_db, harness):
    _seed_claude()
    roster.record_native_identity(
        terminal_id=TERMINAL_ID,
        native_session_id="11111111-2222-4333-8444-555555555555",
        harness="claude_code",
        generation=GENERATION,
        acquisition_method=native_attachment.ACQUISITION_STATUS_DISCOVERED,
    )
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())

    outcome = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID, generation=GENERATION, provider_version=CLAUDE_VERSION
    )
    _assert_escape_contract(
        harness, outcome, expected_status="refused", expected_reason="identity-conflict"
    )
    assert _current_lineage()["native_session_id"] == "11111111-2222-4333-8444-555555555555"


def test_claude_escape_exactly_once_on_attachment_conflict(isolated_memory_db, harness):
    _seed_claude()
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())
    # Another live owner already holds the provider session.
    _seed_terminal("claude_code", terminal_id="d4e5f607", generation=_uuid())
    native_attachment.declare(
        provider="claude_code",
        native_session_id=SESSION_ID,
        terminal_id="d4e5f607",
        generation=_uuid(),
        execution_mode=em.NATIVE_TUI,
        intent=native_attachment.acquire_intent(
            acquisition_method=native_attachment.ACQUISITION_CHOSEN_SESSION_ID,
            acquisition_receipt={"schema": "test-intent", "note": "other owner"},
            admits_only_new_instructions=True,
            replays_task_bytes=False,
        ),
    )

    outcome = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID, generation=GENERATION, provider_version=CLAUDE_VERSION
    )
    _assert_escape_contract(
        harness, outcome, expected_status="refused", expected_reason="attachment-conflict"
    )
    # Zero identity mutation: neither the terminal row nor the roster moved.
    assert _terminal_row().v2_native_session_id is None
    assert _current_lineage()["native_session_id"] is None
    assert _evidence_rows() == []


def test_claude_escape_exactly_once_when_composer_proof_fails(isolated_memory_db, harness):
    _seed_claude()
    harness.screens.append(claude_panel_rows())
    harness.composer_proof_rows = ["still showing the modal", "Esc to cancel"]

    outcome = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID, generation=GENERATION, provider_version=CLAUDE_VERSION
    )
    _assert_escape_contract(
        harness, outcome, expected_status="refused", expected_reason="composer-not-restored"
    )
    # The identity was observed but never committed.
    assert _terminal_row().v2_native_session_id is None
    assert _evidence_rows() == []


def test_claude_escape_exactly_once_on_persistence_failure(
    isolated_memory_db, harness, monkeypatch
):
    _seed_claude()
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())
    monkeypatch.setattr(
        database,
        "set_terminal_native_session_id_conditional",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db exploded")),
    )

    outcome = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID, generation=GENERATION, provider_version=CLAUDE_VERSION
    )
    _assert_escape_contract(
        harness, outcome, expected_status="refused", expected_reason="persistence-failed"
    )
    # The attachment adoption is conservative: the exclusive owner remains.
    attachment = native_attachment.get("claude_code", SESSION_ID)
    assert attachment is not None and attachment["state"] == native_attachment.ATTACHED
    assert _terminal_row().v2_native_session_id is None


def test_claude_escape_exactly_once_on_cancellation(isolated_memory_db, harness):
    _seed_claude()
    # Cancellation lands inside the panel capture: BaseException semantics,
    # so the Escape still runs in the finally and the lease is released.
    harness.capture_errors.append(asyncio.CancelledError("cancelled"))

    with pytest.raises(asyncio.CancelledError):
        nsr.repair_terminal_native_identity(
            terminal_id=TERMINAL_ID, generation=GENERATION, provider_version=CLAUDE_VERSION
        )
    _assert_escape_contract(harness, None, expected_status=None, expected_reason=None)
    assert _terminal_row().v2_native_session_id is None


def test_cleanup_failure_never_turns_primary_failure_into_success(
    isolated_memory_db, harness, monkeypatch
):
    # The panel never parses (the primary failure) while the Escape also
    # fails: the primary panel-unparsed refusal must be preserved, never
    # masked into success or into a different refusal.
    _seed_claude()
    monkeypatch.setattr(v2, "NATIVE_PANE_READY_TIMEOUT_SECONDS", 0.15)
    harness.screens.append(["garbage that never parses"])

    def _key_failure(self, keystroke):
        self._state.typed_key(keystroke)
        raise RuntimeError("Escape refused by tmux")

    monkeypatch.setattr(_FakeTmuxPaneInput, "send_key", _key_failure)

    outcome = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID, generation=GENERATION, provider_version=CLAUDE_VERSION
    )
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "panel-unparsed"
    assert harness.escapes == 1
    assert _terminal_row().v2_native_session_id is None


def test_escape_failure_alone_never_reports_success(isolated_memory_db, harness, monkeypatch):
    # The panel parsed fine but the single Escape failed: without the
    # post-Escape styled composer proof nothing is ever reported ready and
    # nothing is committed (the DB writer is never even reached).
    _seed_claude()
    harness.screens.append(claude_panel_rows())

    def _key_failure(self, keystroke):
        self._state.typed_key(keystroke)
        raise RuntimeError("Escape refused by tmux")

    monkeypatch.setattr(_FakeTmuxPaneInput, "send_key", _key_failure)

    outcome = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID, generation=GENERATION, provider_version=CLAUDE_VERSION
    )
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "composer-not-restored"
    assert harness.escapes == 1
    assert _terminal_row().v2_native_session_id is None
    assert _current_lineage()["native_session_id"] is None
    assert _evidence_rows() == []


# ---------------------------------------------------------------------------
# Operation: contention, drift, and idempotence
# ---------------------------------------------------------------------------


def test_lease_contention_writes_zero_bytes_and_zero_mutation(isolated_memory_db, harness):
    _seed_all("claude_code")
    harness.screens.append(claude_panel_rows())
    # Another writer holds the pane lease from a different thread (a
    # same-thread holder would be a reentry programming error, not the
    # contention this test is about).
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
        outcome = nsr.repair_terminal_native_identity(
            terminal_id=TERMINAL_ID, generation=GENERATION, provider_version=CLAUDE_VERSION
        )
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
    outcome = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID, generation=GENERATION, provider_version=CLAUDE_VERSION
    )
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

    outcome = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID, generation=GENERATION, provider_version=CLAUDE_VERSION
    )
    assert outcome["status"] == "refused"
    assert outcome["reason"] == expected_reason
    assert harness.typed == []
    assert _terminal_row().v2_native_session_id is None


def test_process_identity_drift_is_refused(isolated_memory_db, harness):
    _seed_terminal("claude_code")
    _seed_roster("claude_code", start_marker="Mon Jan 1 00:00:00 2024")
    harness.screens.append(claude_panel_rows())

    outcome = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID, generation=GENERATION, provider_version=CLAUDE_VERSION
    )
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "process-identity-drift"
    assert harness.typed == []


def test_retired_or_missing_roster_incarnation_refuses(isolated_memory_db, harness):
    _seed_terminal("claude_code")
    _seed_roster("claude_code")
    roster.retire_incarnation(terminal_id=TERMINAL_ID, generation=GENERATION, reason="stop")
    harness.screens.append(claude_panel_rows())
    outcome = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID, generation=GENERATION, provider_version=CLAUDE_VERSION
    )
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "incarnation-retired"
    assert harness.typed == []

    # A terminal with no roster row at all is refused the same way.
    orphan_id, orphan_gen = "e5f60708", _uuid()
    _seed_terminal("codex", terminal_id=orphan_id, generation=orphan_gen)
    outcome = nsr.repair_terminal_native_identity(
        terminal_id=orphan_id, generation=orphan_gen, provider_version=CODEX_VERSION
    )
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "no-roster-incarnation"


def test_unsupported_build_and_provider_refuse_before_any_io(isolated_memory_db, harness):
    _seed_all("claude_code")
    outcome = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID, generation=GENERATION, provider_version="9.9.9"
    )
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "unsupported-build"
    assert harness.typed == []

    other_id, other_gen = "f6070819", _uuid()
    _seed_terminal("kiro_cli", terminal_id=other_id, generation=other_gen)
    outcome = nsr.repair_terminal_native_identity(
        terminal_id=other_id, generation=other_gen, provider_version="1.0.0"
    )
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "provider-unsupported"
    assert harness.typed == []


def test_stored_same_id_is_idempotent_replay(isolated_memory_db, harness):
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())
    _seed_terminal("claude_code", native_session_id=SESSION_ID)
    _seed_roster("claude_code", native_session_id=SESSION_ID)

    outcome = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID, generation=GENERATION, provider_version=CLAUDE_VERSION
    )
    assert outcome["status"] == "repaired"
    assert outcome["native_session_id"] == SESSION_ID
    assert _terminal_row().v2_native_session_id == SESSION_ID


def test_stored_different_id_is_never_overwritten(isolated_memory_db, harness):
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())
    _seed_terminal("claude_code", native_session_id="11111111-2222-4333-8444-555555555555")
    _seed_roster("claude_code", native_session_id="11111111-2222-4333-8444-555555555555")

    outcome = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID, generation=GENERATION, provider_version=CLAUDE_VERSION
    )
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "identity-conflict"
    assert _terminal_row().v2_native_session_id == "11111111-2222-4333-8444-555555555555"
    assert _evidence_rows() == []


def test_legacy_terminal_model_row_repairs_through_the_roster_anchor(isolated_memory_db, harness):
    """A pre-v2 terminal row (no generation column) still repairs: the exact
    roster incarnation is the generation anchor and the pane tuple proves
    the row is the same physical pane."""
    with database.SessionLocal() as db:
        db.add(
            database.TerminalModel(
                id=TERMINAL_ID,
                tmux_session=SESSION_NAME,
                tmux_window=f"w-{TERMINAL_ID}",
                provider="claude_code",
                generation=None,
                pane_id=PANE_ID,
                window_id=WINDOW_ID,
                server_socket_path=SERVER_SOCKET,
                session_id=TMUX_SESSION_ID,
                pane_pid=PANE_PID,
                lifecycle_state="live",
            )
        )
        db.commit()
    _seed_roster("claude_code")
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())

    outcome = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID, generation=GENERATION, provider_version=CLAUDE_VERSION
    )
    assert outcome["status"] == "repaired"
    with database.SessionLocal() as db:
        row = (
            db.query(database.TerminalModel)
            .filter(database.TerminalModel.id == TERMINAL_ID)
            .first()
        )
        assert row.native_session_id == SESSION_ID


# ---------------------------------------------------------------------------
# Attachment adoption contract
# ---------------------------------------------------------------------------


def _declare_other_owner() -> None:
    _seed_terminal("claude_code", terminal_id="d4e5f607", generation=_uuid())
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


def test_attachment_other_live_owner_refuses_before_identity_mutation(isolated_memory_db, harness):
    _seed_all("claude_code")
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())
    _declare_other_owner()

    outcome = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID, generation=GENERATION, provider_version=CLAUDE_VERSION
    )
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "attachment-conflict"
    assert _terminal_row().v2_native_session_id is None
    assert _current_lineage()["native_session_id"] is None


def test_attachment_frozen_ambiguous_refuses(isolated_memory_db, harness):
    _seed_all("claude_code")
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())
    _declare_other_owner()
    native_attachment.mark_ambiguous(
        provider="claude_code",
        native_session_id=SESSION_ID,
        reason="operator froze it",
    )

    outcome = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID, generation=GENERATION, provider_version=CLAUDE_VERSION
    )
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "attachment-conflict"
    assert _terminal_row().v2_native_session_id is None


def test_attachment_exact_same_owner_adopts_idempotently(isolated_memory_db, harness):
    _seed_all("claude_code")
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())

    first = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID, generation=GENERATION, provider_version=CLAUDE_VERSION
    )
    second = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID, generation=GENERATION, provider_version=CLAUDE_VERSION
    )
    assert first["status"] == second["status"] == "repaired"
    assert first["operation_id"] != second["operation_id"]
    # One attachment, one receipt (the first), never overwritten.
    attachment = native_attachment.get("claude_code", SESSION_ID)
    assert attachment["owner"]["terminal_id"] == TERMINAL_ID
    assert attachment["adoption_receipt"]["operation_id"] == first["operation_id"]
    assert len(_evidence_rows()) == 2


def test_failure_after_attachment_is_conservative_and_exact_retry_converges(
    isolated_memory_db, harness, monkeypatch
):
    _seed_all("claude_code")
    harness.screens.append(claude_panel_rows())
    harness.styled_screens.append(claude_composer_rows())
    real_commit = nsr._commit_repair
    calls = {"n": 0}

    def _fail_once(db, facts):
        calls["n"] += 1
        if calls["n"] == 1:
            raise nsr.NativeStatusRepairUnavailable("row+roster commit failed")
        return real_commit(db, facts)

    monkeypatch.setattr(nsr, "_commit_repair", _fail_once)

    first = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID, generation=GENERATION, provider_version=CLAUDE_VERSION
    )
    assert first["status"] == "refused"
    assert first["reason"] == "persistence-failed"
    # Conservative: the exclusive attachment remains visible and safe.
    attachment = native_attachment.get("claude_code", SESSION_ID)
    assert attachment is not None and attachment["state"] == native_attachment.ATTACHED
    assert _terminal_row().v2_native_session_id is None

    # An exact retry converges without another /status: the prior adoption
    # already names this exact pane/process identity.
    harness.typed.clear()
    harness.calls.clear()
    second = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID, generation=GENERATION, provider_version=CLAUDE_VERSION
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

    # Roster side fails inside the shared transaction -> terminal row and
    # evidence must roll back with it.
    real_record = roster.record_native_identity

    def _fail_roster(**kwargs):
        if kwargs.get("db") is not None:
            raise roster.StableAgentUnavailable("roster store exploded")
        return real_record(**kwargs)

    monkeypatch.setattr(roster, "record_native_identity", _fail_roster)
    outcome = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID, generation=GENERATION, provider_version=CLAUDE_VERSION
    )
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "persistence-failed"
    assert _terminal_row().v2_native_session_id is None
    assert _evidence_rows() == []
    assert _current_lineage()["native_session_id"] is None


def test_operation_never_touches_teardown_or_delivery(isolated_memory_db, harness, monkeypatch):
    from cli_agent_orchestrator.services import terminal_service

    _seed_claude()
    harness.screens.append(claude_panel_rows())

    def _loud(*args, **kwargs):
        raise AssertionError("the repair must never touch teardown or delivery")

    monkeypatch.setattr(terminal_service, "delete_terminal", _loud)
    monkeypatch.setattr(native_attachment, "release", _loud)
    monkeypatch.setattr(native_attachment, "mark_ambiguous", _loud)
    monkeypatch.setattr(native_attachment, "mark_draining", _loud)

    outcome = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID, generation=GENERATION, provider_version=CLAUDE_VERSION
    )
    assert outcome["status"] == "refused"  # the unparseable panel is refused
    # Even after a failed repair the pane lease is free, so teardown is not
    # blocked: acquiring with a zero timeout succeeds.
    with pia.pane_input_lease(PANE_ID, holder="teardown-check", timeout=0.0):
        pass


# ---------------------------------------------------------------------------
# Input contract
# ---------------------------------------------------------------------------


def test_operation_requires_exact_terminal_id_and_generation(isolated_memory_db, harness):
    outcome = nsr.repair_terminal_native_identity(
        terminal_id="", generation=GENERATION, provider_version=CLAUDE_VERSION
    )
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "invalid-input"

    outcome = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID, generation="", provider_version=CLAUDE_VERSION
    )
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "invalid-input"


def test_terminal_not_found_is_typed(isolated_memory_db, harness):
    outcome = nsr.repair_terminal_native_identity(
        terminal_id=TERMINAL_ID, generation=GENERATION, provider_version=CLAUDE_VERSION
    )
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "terminal-not-found"
    assert harness.typed == []
