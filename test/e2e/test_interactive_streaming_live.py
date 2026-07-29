"""§10.3 r15 interactive-streaming installed live-provider acceptance.

Drives the declared interactive streaming path (design §6.7, cond-0194)
end-to-end against disposable managed native-TUI sessions on the pinned
provider builds (kimi 0.29.2, claude 2.1.220), reusing the same harness
discipline as the Lane C §10.6 file (private tmux socket, temp
CAO_STATE_ROOT, real $HOME for provider auth):

* undeclared automation stays readiness-gated during an active turn —
  the interactive bypass is never inherited (the inheritance fence);
* declared interactive text queues/enters mid-turn; navigation/menu key
  sequences and Escape deliver; kimi C-s steering delivers mid-turn;
* per-build menu honesty (r16): on kimi 0.29.2 the mid-turn /model menu
  opens through the declared path, navigates, and Escape safe-cancels —
  overlay closed, setting unchanged by authoritative rereads, turn
  continuing; on claude 2.1.220 (a live-proven provider limit, not a CAO
  defect) the mid-turn /model is accepted and queued in the native
  composer — no menu opens, the setting is unchanged, the turn continues,
  and the evidence claims bytes delivered / a queued command only, never
  menu execution or cancellation (a future build that opens a real menu
  here fails loudly for re-pinning);
* the kimi steer effect (Fable r15 P2, Sol r16 P1.2): queued mid-turn
  instruction text + declared C-s — the pending queue empties into the
  turn context and FRESH provider-output rows carry the unique requested
  suffix (numeric lines only; instruction/queue echoes and pre-steer
  capture excluded by the shape predicate);
* true pane-input lease contention refuses truthfully with the §6.4
  discriminator detail (never a bypass of a real lease owner);
* stale identity and copy mode are zero-byte refusals for declared
  interactive batches, exactly as for any other batch;
* a killed response mid-submit reconciles by one exact-id GET to the
  journaled accepted answer — never a resend, exactly one write.

Evidence (sanitized request/response JSON + transcript captures) is written
under ``$CAO_LANE_C_EVIDENCE_DIR`` or a per-run scratch dir.

Run with:

    uv run pytest -m e2e test/e2e/test_interactive_streaming_live.py -v
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path
from test.e2e.test_native_tui_provider_acceptance import (
    Evidence,
    Harness,
    ProviderSession,
    _await,
    _build_kimi_provider_home_shim,
    _capture,
    _control_identity,
    _harvest_email_tokens,
    _kill_session,
    _kimi_menu_open,
    _launch_provider_session,
    _post,
    _post_events,
    _turn_active,
)
from test.e2e.test_operator_message_live import (
    CLAUDE_MODEL_ALIAS,
    CLAUDE_PIN,
    EFFORT_PROVIDER_DEFAULT,
    KIMI_MODEL,
    KIMI_PIN,
    LANE_C_PROFILE,
    TURN_TIMEOUT,
    _expected_identity,
    _harvest_account_display_names,
    _post_killing_response,
    _wait_turn_done,
)
from test.fixtures.cao_server import _pick_free_port, _start_cao_server
from test.fixtures.tmux_server import TmuxServer, isolated_tmux_server
from typing import Any, Dict, Iterator

import pytest
import requests

pytestmark = pytest.mark.e2e

EVIDENCE_ENV = "CAO_LANE_C_EVIDENCE_DIR"

_SCRUB_EXACT = {
    "CAO_TERMINAL_ID",
    "CAO_ALLOWED_HOSTS",
    "CAO_WS_ALLOWED_CLIENTS",
    "KIMI_MODEL_THINKING_EFFORT",
}
_SCRUB_PREFIXES = ("CAO_CONDUCTOR_", "CAO_WORKFLOW_")

#: The slow prompt that keeps a provider turn reliably active for the
#: mid-turn scenarios.
_LONG_TURN_PROMPT = (
    "Count from 1 to 120, one number per line, thinking briefly between "
    "lines. Work slowly and do not stop early."
)


@pytest.fixture(scope="session")
def tmux_server() -> Iterator[TmuxServer]:
    if not shutil.which("tmux"):
        pytest.skip("tmux not installed")
    with isolated_tmux_server() as server:
        yield server


@pytest.fixture(scope="session")
def harness(tmp_path_factory: pytest.TempPathFactory, tmux_server: TmuxServer) -> Iterator[Harness]:
    real_home = os.environ.get("HOME", "")
    if not real_home or not Path(real_home).is_dir():
        pytest.skip("§10.3 acceptance needs the operator's real $HOME (provider auth)")
    home_path = Path(real_home)

    state_root = Path(tmp_path_factory.mktemp("cao_state_r15"))
    scratch = Path(tmp_path_factory.mktemp("cao_scratch_r15"))
    server_bookkeeping = scratch / "server-home"

    agent_store = state_root / "agent-store"
    agent_store.mkdir(parents=True, exist_ok=True)
    (agent_store / f"{LANE_C_PROFILE}.md").write_text(
        "---\n"
        "name: lanec-acceptance\n"
        "description: Disposable Lane C 10.6 acceptance profile (no MCP servers)\n"
        "---\n\n"
        "You are a disposable acceptance-test agent.  Keep every reply as short as\n"
        "possible — one short sentence when you can.\n",
        encoding="utf-8",
    )

    kimi_home_shim = _build_kimi_provider_home_shim(home_path, scratch)

    evidence_root = Path(os.environ.get(EVIDENCE_ENV) or (scratch / "evidence"))
    evidence = Evidence(evidence_root)
    evidence.redact(real_home, "<HOME>")
    evidence.redact(str(scratch), "<SCRATCH>")
    evidence.redact(str(state_root), "<STATE_ROOT>")
    evidence.redact(str(tmux_server.owned_root), "<TMUX_SOCKDIR>")
    evidence.redact(os.environ.get("USER", ""), "<USER>")
    # The machine temp root itself (r1, cond steers 109/112): transcripts
    # render box-truncated paths the exact scratch strings never match, so
    # the tmp-root prefix is redacted wholesale — in BOTH the raw
    # (/var/folders/...) and resolved (/private/var/folders/...) forms,
    # because macOS symlinks /var and transcripts show the resolved one.
    # A rerun cannot reintroduce raw tmp paths.
    evidence.redact(tempfile.gettempdir(), "<HOST_TMP>")
    evidence.redact(os.path.realpath(tempfile.gettempdir()), "<HOST_TMP>")
    for token in _harvest_email_tokens(
        [home_path / ".kimi-code" / "config.toml", home_path / ".claude.json"]
    ):
        evidence.redact(token, "<ACCOUNT>")
    for token in _harvest_account_display_names(home_path):
        evidence.redact(token, "<ACCOUNT>")

    assert tmux_server.owned_root is not None
    shim = tmux_server.write_shim(tmux_server.owned_root / "bin")

    saved: Dict[str, str] = {}
    for name in list(os.environ):
        if name in _SCRUB_EXACT or name.startswith(_SCRUB_PREFIXES):
            saved[name] = os.environ.pop(name)
    try:
        server = _start_cao_server(
            server_bookkeeping,
            _pick_free_port(),
            extra_env={
                "HOME": real_home,
                "CAO_STATE_ROOT": str(state_root),
                "KIMI_CODE_HOME": str(kimi_home_shim),
                "PATH": tmux_server.subprocess_env(shim)["PATH"],
            },
            deadline=30.0,
        )
    finally:
        os.environ.update(saved)

    bundle = Harness(
        server=server,
        tmux=tmux_server,
        state_root=state_root,
        scratch=scratch,
        evidence=evidence,
    )
    try:
        yield bundle
    finally:
        server.stop()


@pytest.fixture(scope="module")
def kimi_session(harness: Harness) -> Iterator[ProviderSession]:
    session = _launch_provider_session(
        harness,
        provider="kimi_cli",
        binary="kimi",
        pin=KIMI_PIN,
        expected_model=KIMI_MODEL,
        expected_effort=EFFORT_PROVIDER_DEFAULT,
        tag="r15-kimi",
        agent_profile=LANE_C_PROFILE,
    )
    try:
        yield session
    finally:
        _kill_session(harness, session)


@pytest.fixture(scope="module")
def claude_session(harness: Harness) -> Iterator[ProviderSession]:
    session = _launch_provider_session(
        harness,
        provider="claude_code",
        binary="claude",
        pin=CLAUDE_PIN,
        expected_model=CLAUDE_MODEL_ALIAS,
        expected_effort=EFFORT_PROVIDER_DEFAULT,
        tag="r15-claude",
        agent_profile=LANE_C_PROFILE,
    )
    try:
        yield session
    finally:
        _kill_session(harness, session)


# ---------------------------------------------------------------------------
# Scenario helpers
# ---------------------------------------------------------------------------


def _identity_block(harness: Harness, session: ProviderSession, provider: str) -> Dict[str, Any]:
    identity = _control_identity(harness, session)
    return identity["control_input"]["provider_controls"][provider]


def _start_long_turn(harness: Harness, session: ProviderSession) -> None:
    """Submit the slow counting prompt (undeclared prose, sent while idle)
    and wait until the provider turn is observably active."""
    _wait_turn_done(harness, session)
    _post(
        harness,
        session,
        {
            "text": _LONG_TURN_PROMPT,
            "enter": True,
            "expected_identity": _expected_identity(harness, session),
        },
    )
    assert _await(
        lambda: _turn_active(harness, session), timeout=60.0, poll=0.5
    ), "the long turn never became observably active"


def _stop_turn(harness: Harness, session: ProviderSession) -> None:
    _post_events(harness, session, [{"type": "key", "key": "Escape"}])
    _wait_turn_done(harness, session)


def _transcript_count(harness: Harness, session: ProviderSession, marker: str) -> int:
    return _capture(harness, session).count(marker)


def _turn_progress(transcript: str) -> int:
    """Distinct pure-number lines: the counting turn's progress meter.  A
    silently interrupted turn stalls this; a healthy one keeps growing it."""
    return len(set(re.findall(r"(?m)^\s*(\d{1,3})\s*$", transcript)))


def _bottom_rows(transcript: str, rows: int = 10) -> str:
    return "\n".join(transcript.splitlines()[-rows:])


def _shim_config_model(harness: Harness) -> Any:
    """The persisted `model` value in the kimi shim-home config copy (the
    file a `/model` switch would persist into — never the operator's)."""
    config = Path(harness.scratch) / "kimi-provider-home" / "config.toml"
    try:
        match = re.search(r'(?m)^model\s*=\s*"([^"]+)"', config.read_text(encoding="utf-8"))
    except OSError:
        return None
    return match.group(1) if match else None


def _claude_config_model() -> Any:
    """The persisted `model` key in the operator's ~/.claude.json — read
    and compared in-test, never printed (values stay out of evidence)."""
    try:
        data = json.loads(
            (Path(os.environ.get("HOME", "")) / ".claude.json").read_text(
                encoding="utf-8", errors="replace"
            )
        )
    except (OSError, json.JSONDecodeError):
        return None
    return data.get("model")


#: Markers of Claude's /model selector overlay (2.1.220).
_CLAUDE_MENU_HINTS = ("Select model", "Choose a model", "Model selection")


def _claude_menu_open(harness: Harness, session: ProviderSession) -> bool:
    screen = _capture(harness, session)
    return any(hint in screen for hint in _CLAUDE_MENU_HINTS)


def _await_journal_terminal(harness: Harness, control_id: str, timeout: float = 60.0) -> str:
    """Wait — on local journal evidence, never the reconcile API — for the
    control's record to reach a terminal state, so the ONE exact-id GET
    that follows answers the terminal truth (§6.7/§8.3: one exact-id
    reconcile, never a resend, and no GET polling either)."""
    db = Path(harness.state_root) / "db" / "control-input.sqlite3"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if db.exists():
            try:
                connection = sqlite3.connect(str(db), timeout=5)
                try:
                    row = connection.execute(
                        "SELECT state FROM control_input_request WHERE request_id = ?",
                        (control_id,),
                    ).fetchone()
                finally:
                    connection.close()
            except sqlite3.Error:
                row = None
            if row is not None and row[0] in ("delivered", "refused", "ambiguous"):
                return str(row[0])
        time.sleep(0.25)
    raise AssertionError(f"no terminal journal record for {control_id} after the killed response")


class TestKimiInteractiveStreaming:
    """§6.7 on the pinned kimi 0.29.2 build, turn active."""

    def test_01_capability_and_undeclared_gate(
        self, harness: Harness, kimi_session: ProviderSession
    ):
        case = "r15-kimi-01-capability-and-gate"
        block = _identity_block(harness, kimi_session, "kimi_cli")
        assert block["interactive_streaming"] == {"supported": True}
        harness.evidence.write_json(case, "identity-provider-controls.json", block)

        _start_long_turn(harness, kimi_session)
        # The inheritance fence: an UNDECLARED composer batch of the same
        # shape stays readiness-gated during the active turn.
        request, response = _post_events(
            harness, kimi_session, [{"type": "text", "text": "automation prose"}]
        )
        harness.evidence.write_json(case, "undeclared-request.json", request)
        harness.evidence.write_json(case, "undeclared-response.json", response)
        assert response["outcome"] == "refused", response
        assert response["reason_code"] == "pane-busy", response
        harness.evidence.write(
            case, "10-transcript-active-turn.txt", _capture(harness, kimi_session)
        )
        _stop_turn(harness, kimi_session)

    def test_02_interactive_text_queues_and_enters_mid_turn(
        self, harness: Harness, kimi_session: ProviderSession
    ):
        case = "r15-kimi-02-interactive-text"
        _start_long_turn(harness, kimi_session)
        marker = f"R15QUEUE-{uuid.uuid4().hex[:8]}"

        # A text-only interactive batch types into the composer mid-turn.
        request, response = _post_events(
            harness, kimi_session, [{"type": "text", "text": marker}], payload_class="interactive"
        )
        harness.evidence.write_json(case, "queue-request.json", request)
        harness.evidence.write_json(case, "queue-response.json", response)
        assert response["outcome"] == "accepted", response
        assert response.get("request_schema_version") == 4, response
        assert _await(
            lambda: marker in _capture(harness, kimi_session), timeout=30.0
        ), "the interactive text never reached the mid-turn composer"

        # The Enter submits it once — queued per the provider's mid-turn
        # semantics; the wire fact is the accepted batch.
        request, response = _post_events(
            harness, kimi_session, [{"type": "key", "key": "Enter"}], payload_class="interactive"
        )
        harness.evidence.write_json(case, "enter-request.json", request)
        harness.evidence.write_json(case, "enter-response.json", response)
        assert response["outcome"] == "accepted", response
        assert _await(
            lambda: _transcript_count(harness, kimi_session, marker) >= 1,
            timeout=30.0,
        )
        harness.evidence.write(case, "10-transcript.txt", _capture(harness, kimi_session))
        _stop_turn(harness, kimi_session)

    def test_03_interactive_navigation_and_menu_escape_deliver(
        self, harness: Harness, kimi_session: ProviderSession
    ):
        case = "r15-kimi-03-navigation"
        _start_long_turn(harness, kimi_session)
        for name, events in {
            "arrows": [{"type": "key", "key": "Down"}, {"type": "key", "key": "Up"}],
            "escape": [{"type": "key", "key": "Escape"}],
        }.items():
            request, response = _post_events(
                harness, kimi_session, events, payload_class="interactive"
            )
            harness.evidence.write_json(case, f"{name}-request.json", request)
            harness.evidence.write_json(case, f"{name}-response.json", response)
            assert response["outcome"] == "accepted", (name, response)
        harness.evidence.write(case, "10-transcript.txt", _capture(harness, kimi_session))
        _stop_turn(harness, kimi_session)

    def test_04_interactive_c_s_steer_delivers_mid_turn(
        self, harness: Harness, kimi_session: ProviderSession
    ):
        case = "r15-kimi-04-steer"
        _start_long_turn(harness, kimi_session)
        request, response = _post_events(
            harness, kimi_session, [{"type": "chord", "chord": "C-s"}], payload_class="interactive"
        )
        harness.evidence.write_json(case, "steer-request.json", request)
        harness.evidence.write_json(case, "steer-response.json", response)
        assert response["outcome"] == "accepted", response
        harness.evidence.write(case, "10-transcript.txt", _capture(harness, kimi_session))
        _stop_turn(harness, kimi_session)

    def test_05_true_lease_contention_refuses_truthfully(
        self, harness: Harness, kimi_session: ProviderSession
    ):
        case = "r15-kimi-05-lease-contention"
        _start_long_turn(harness, kimi_session)
        # A genuinely held cross-process lease: the test process takes the
        # arbiter's flock for this exact pane (the same file the server's
        # writer must take), so the declared interactive POST meets a real
        # owner — deterministically, with no timing race (§6.7: the bypass
        # never skips pane lease contention).
        pane = kimi_session.pane_id
        lock_dir = Path(harness.state_root) / "pane-input-locks"
        lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(str(lock_dir / f"pane-{pane[1:]}.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            request, response = _post_events(
                harness,
                kimi_session,
                [{"type": "key", "key": "Down"}],
                payload_class="interactive",
            )
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        harness.evidence.write_json(case, "contended-request.json", request)
        harness.evidence.write_json(case, "contended-response.json", response)
        harness.evidence.note(
            case,
            "the test process held the arbiter flock for the exact pane; "
            "the interactive POST met a real cross-process lease owner",
        )
        assert response["outcome"] == "refused", response
        assert response["reason_code"] == "pane-busy", response
        assert "input lease is held by" in (response.get("detail") or ""), response
        harness.evidence.write(case, "10-transcript.txt", _capture(harness, kimi_session))
        _stop_turn(harness, kimi_session)

    def test_06_stale_identity_is_a_zero_byte_refusal(
        self, harness: Harness, kimi_session: ProviderSession
    ):
        case = "r15-kimi-06-stale-identity"
        _start_long_turn(harness, kimi_session)
        marker = f"R15STALE-{uuid.uuid4().hex[:8]}"
        tampered = {
            **_expected_identity(harness, kimi_session),
            "terminal_generation": "00000000-0000-0000-0000-000000000000",
        }
        body: Dict[str, Any] = {
            "events": [{"type": "text", "text": marker}],
            "payload_class": "interactive",
            "expected_identity": tampered,
        }
        request, response = _post(harness, kimi_session, body)
        harness.evidence.write_json(case, "stale-request.json", request)
        harness.evidence.write_json(case, "stale-response.json", response)
        assert response["outcome"] == "refused", response
        assert response["reason_code"] in ("stale-generation", "identity-mismatch"), response
        assert marker not in _capture(harness, kimi_session), "zero bytes were not zero"
        harness.evidence.write(case, "10-transcript.txt", _capture(harness, kimi_session))
        _stop_turn(harness, kimi_session)

    def test_07_copy_mode_is_a_fail_closed_zero_byte_refusal(
        self, harness: Harness, kimi_session: ProviderSession
    ):
        """§6.7: a declared interactive batch refuses fail-closed on a
        proven copy mode — zero bytes, and the operator's copy mode left
        untouched (legacy undeclared auto-exit is not the interactive rule)."""
        case = "r15-kimi-07-copy-mode"
        _start_long_turn(harness, kimi_session)
        marker = f"R15COPY-{uuid.uuid4().hex[:8]}"
        harness.tmux.out("copy-mode", "-t", kimi_session.pane_id)
        try:
            request, response = _post_events(
                harness,
                kimi_session,
                [{"type": "text", "text": marker}],
                payload_class="interactive",
            )
            still_in_mode = harness.tmux.out(
                "display-message", "-p", "-t", kimi_session.pane_id, "#{pane_in_mode}"
            )
        finally:
            harness.tmux.run("send-keys", "-t", kimi_session.pane_id, "-X", "cancel", check=False)
        harness.evidence.write_json(case, "copy-mode-request.json", request)
        harness.evidence.write_json(case, "copy-mode-response.json", response)
        harness.evidence.note(case, f"pane_in_mode after the refused POST: {still_in_mode}")
        assert response["outcome"] == "refused", response
        assert response["reason_code"] == "copy-mode-active", response
        assert still_in_mode.strip() == "1", "the guard exited the operator's copy mode"
        assert marker not in _capture(harness, kimi_session), "zero bytes were not zero"
        harness.evidence.write(case, "10-transcript.txt", _capture(harness, kimi_session))
        _stop_turn(harness, kimi_session)

    def test_08_killed_response_reconciles_exact_id_never_resend(
        self, harness: Harness, kimi_session: ProviderSession
    ):
        case = "r15-kimi-08-killed-response"
        _start_long_turn(harness, kimi_session)
        marker = f"R15KILL-{uuid.uuid4().hex[:8]}"
        control_id = f"ctl-{uuid.uuid4().hex[:10]}"
        body: Dict[str, Any] = {
            "control_id": control_id,
            "events": [{"type": "text", "text": marker}, {"type": "key", "key": "Enter"}],
            "payload_class": "interactive",
            "expected_identity": _expected_identity(harness, kimi_session),
        }
        harness.evidence.write_json(case, "submit-killed-request.json", body)
        note = _post_killing_response(
            harness, f"/terminals/{kimi_session.terminal_id}/control-input", body
        )
        harness.evidence.note(case, note)

        assert _await(
            lambda: _transcript_count(harness, kimi_session, marker) >= 1,
            timeout=60.0,
            poll=1.0,
        ), "the killed-response interactive batch never reached the provider"

        # Settle proven on local journal evidence, then exactly one
        # exact-id reconcile — never a resend, never GET polling.
        state = _await_journal_terminal(harness, control_id)
        harness.evidence.note(
            case, f"journal reached terminal state {state!r} before the one reconcile"
        )
        reconcile = requests.get(f"{harness.server.url}/control-input/{control_id}", timeout=30)
        assert reconcile.status_code == 200
        outcome = reconcile.json()
        harness.evidence.write_json(case, "reconcile-response.json", outcome)
        assert outcome["outcome"] == "accepted", outcome

        count = _transcript_count(harness, kimi_session, marker)
        harness.evidence.note(case, f"marker occurrences in transcript: {count}")
        assert count == 1, f"marker appears {count} times — a duplicate submission happened"
        harness.evidence.write(case, "10-transcript.txt", _capture(harness, kimi_session))
        _stop_turn(harness, kimi_session)

    def test_09_mid_turn_model_menu_safe_cancel(
        self, harness: Harness, kimi_session: ProviderSession
    ):
        """Fable r15 P1: open /model during the active turn through the
        declared path, navigate without changing the setting, Escape-cancel
        — the overlay closes, the setting is unchanged by authoritative
        rereads, and the original turn continues (never silently
        interrupted by the cancellation)."""
        case = "r15-kimi-09-menu-safe-cancel"
        _start_long_turn(harness, kimi_session)
        progress_at_open = _turn_progress(_capture(harness, kimi_session))
        model_before = _shim_config_model(harness)
        harness.evidence.note(case, f"model before drill (shim config): {model_before!r}")

        # Open the menu by its real supported interaction, declared interactive.
        request, response = _post_events(
            harness,
            kimi_session,
            [{"type": "text", "text": "/model"}, {"type": "key", "key": "Enter"}],
            payload_class="interactive",
        )
        harness.evidence.write_json(case, "01-model-enter-request.json", request)
        harness.evidence.write_json(case, "02-model-enter-response.json", response)
        assert response["outcome"] == "accepted", response
        harness.evidence.write(case, "10-after-model-enter.txt", _capture(harness, kimi_session))
        menu_open = _await(lambda: _kimi_menu_open(harness, kimi_session), timeout=8.0, poll=0.25)
        if not menu_open:
            harness.evidence.note(
                case,
                "ADJUDICATION EVIDENCE: the /model palette did not open during the active "
                "turn on pinned kimi 0.29.2 — the typed input was handled as queued text "
                "instead. No Escape was sent (mid-turn Escape with no menu open would "
                "interrupt the operator's turn). Safe menu cancellation mid-turn is not "
                "performable on this build; recorded for retained-writer adjudication.",
            )
            _stop_turn(harness, kimi_session)
            pytest.fail(
                "pinned kimi 0.29.2 does not open /model mid-turn — evidence committed; "
                "escalate to the retained writer"
            )
        harness.evidence.write(case, "20-menu-open.txt", _capture(harness, kimi_session))

        # Navigate without changing the setting (down, then back up).
        request, response = _post_events(
            harness,
            kimi_session,
            [{"type": "key", "key": "Down"}, {"type": "key", "key": "Up"}],
            payload_class="interactive",
        )
        harness.evidence.write_json(case, "30-navigate-response.json", response)
        assert response["outcome"] == "accepted", response
        time.sleep(0.5)
        harness.evidence.write(case, "31-menu-navigated.txt", _capture(harness, kimi_session))

        # Escape-cancel and settle the UI.
        request, response = _post_events(
            harness, kimi_session, [{"type": "key", "key": "Escape"}], payload_class="interactive"
        )
        harness.evidence.write_json(case, "40-escape-response.json", response)
        assert response["outcome"] == "accepted", response
        assert _await(
            lambda: not _kimi_menu_open(harness, kimi_session), timeout=8.0, poll=0.25
        ), "the /model overlay did not close on Escape"
        settled = _capture(harness, kimi_session)
        harness.evidence.write(case, "41-after-escape.txt", settled)

        # The setting is unchanged — provider-authoritative reads.
        model_after = _shim_config_model(harness)
        harness.evidence.note(
            case, f"model after drill (shim config): {model_after!r} (before: {model_before!r})"
        )
        assert model_after == model_before, "the drill changed the persisted model setting"
        status = _bottom_rows(settled, 6)
        assert (
            "K2.7 Coding" in status
        ), f"the status line no longer shows the pinned model:\n{status}"
        block = _identity_block(harness, kimi_session, "kimi_cli")
        assert block["interactive_streaming"] == {"supported": True}
        harness.evidence.write_json(case, "50-identity-reread.json", block)

        # The original turn has the provider-correct outcome: it kept going.
        assert _await(
            lambda: _turn_progress(_capture(harness, kimi_session)) > progress_at_open,
            timeout=60.0,
            poll=1.0,
        ), "the original turn did not continue after the menu cancel (silently interrupted?)"
        harness.evidence.write(case, "60-turn-continued.txt", _capture(harness, kimi_session))
        _stop_turn(harness, kimi_session)

    def test_10_declared_steer_consumes_the_queued_text(
        self, harness: Harness, kimi_session: ProviderSession
    ):
        """Fable r15 P2 + Sol r16 P1.2: queue a unique instruction mid-turn
        through the declared path, send declared C-s, and prove the steer
        EFFECT — newly generated provider-output rows carrying the unique
        requested suffix.  The pass predicate never inspects the user
        instruction, the queue/context echo, or pre-steer capture: bare
        accepted chords and echoed instruction text cannot satisfy it."""
        case = "r15-kimi-10-steer-effect"
        _start_long_turn(harness, kimi_session)
        # Let the turn reach steady number production before the steer, so
        # the requested format change would be visible promptly after it.
        assert _await(
            lambda: _turn_progress(_capture(harness, kimi_session)) >= 3,
            timeout=120.0,
            poll=1.0,
        ), "the counting turn never started producing numbers"
        marker = f"STEERME-{uuid.uuid4().hex[:8]}"
        token = f"ZQ{uuid.uuid4().hex[:6]}"
        instruction = (
            f"{marker}: from now on, write each number on its own line "
            f"followed by the token {token}."
        )
        request, response = _post_events(
            harness,
            kimi_session,
            [{"type": "text", "text": instruction}],
            payload_class="interactive",
        )
        harness.evidence.write_json(case, "01-queue-response.json", response)
        assert response["outcome"] == "accepted", response
        assert _await(
            lambda: marker in _bottom_rows(_capture(harness, kimi_session), 10), timeout=30.0
        ), "the instruction never appeared in the mid-turn queue"
        pre_steer = _capture(harness, kimi_session)
        harness.evidence.write(case, "10-queued.txt", pre_steer)

        # The requested effect has one auditable shape: a numeric line
        # carrying the unique suffix.  The instruction row, its echo, and
        # anything pre-steer can never match it.
        effect_pattern = re.compile(rf"(?m)^\s*\d{{1,3}}\s+{re.escape(token)}(?:[\s.,]|$)")
        assert not effect_pattern.search(
            pre_steer
        ), "the requested effect shape already existed before the steer"

        request, response = _post_events(
            harness, kimi_session, [{"type": "chord", "chord": "C-s"}], payload_class="interactive"
        )
        harness.evidence.write_json(case, "20-steer-response.json", response)
        assert response["outcome"] == "accepted", response

        # Consumption (secondary evidence): the pending queue no longer
        # holds the instruction, and it moved into the turn context.
        assert _await(
            lambda: marker not in _bottom_rows(_capture(harness, kimi_session), 10),
            timeout=30.0,
            poll=0.5,
        ), "the queued instruction was never consumed by the steer"
        consumed = _capture(harness, kimi_session)
        harness.evidence.write(case, "30-after-steer.txt", consumed)
        assert marker in "\n".join(
            consumed.splitlines()[:-10]
        ), "the steered instruction never entered the provider's turn context"

        # The pass condition: FRESH provider output carries the requested
        # suffix — rows generated after the steer, in the requested shape.
        # The settled capture is written either way so a non-delivery is
        # auditable from the bundle, never inferred.
        delivered = _await(
            lambda: effect_pattern.search(_capture(harness, kimi_session)),
            timeout=240.0,
            poll=2.0,
        )
        acted = _capture(harness, kimi_session)
        harness.evidence.write(case, "40-provider-acted.txt", acted)
        if not delivered:
            harness.evidence.note(
                case,
                "EFFECT NOT DELIVERED: no freshly generated provider output carried the "
                "requested suffix within the window; the settled capture is in "
                "40-provider-acted.txt for adjudication",
            )
        assert delivered, "no freshly generated provider output carried the requested suffix"
        effect_rows = [row.strip() for row in acted.splitlines() if effect_pattern.search(row)]
        assert effect_rows, "no fresh provider-output rows carry the requested suffix"
        assert not any(
            row in pre_steer for row in effect_rows
        ), "the effect rows predate the steer — echo, not effect"
        harness.evidence.note(
            case,
            f"steer effect proven: {len(effect_rows)} fresh provider-output row(s) carry "
            f"the requested suffix, e.g. {effect_rows[:3]!r}; instruction/queue echoes "
            "and pre-steer capture excluded by the shape predicate",
        )
        _stop_turn(harness, kimi_session)


class TestClaudeInteractiveStreaming:
    """§6.7 on the pinned claude 2.1.220 build, turn active."""

    def test_01_interactive_text_queues_and_undeclared_stays_gated(
        self, harness: Harness, claude_session: ProviderSession
    ):
        case = "r15-claude-01-interactive-text"
        block = _identity_block(harness, claude_session, "claude_code")
        assert block["interactive_streaming"] == {"supported": True}

        _start_long_turn(harness, claude_session)
        request, response = _post_events(
            harness, claude_session, [{"type": "text", "text": "automation prose"}]
        )
        harness.evidence.write_json(case, "undeclared-request.json", request)
        harness.evidence.write_json(case, "undeclared-response.json", response)
        assert response["outcome"] == "refused", response
        assert response["reason_code"] == "pane-busy", response

        marker = f"R15CLAUDE-{uuid.uuid4().hex[:8]}"
        request, response = _post_events(
            harness,
            claude_session,
            [{"type": "text", "text": marker}, {"type": "key", "key": "Enter"}],
            payload_class="interactive",
        )
        harness.evidence.write_json(case, "interactive-request.json", request)
        harness.evidence.write_json(case, "interactive-response.json", response)
        assert response["outcome"] == "accepted", response
        assert _await(
            lambda: _transcript_count(harness, claude_session, marker) >= 1,
            timeout=60.0,
            poll=1.0,
        ), "the interactive text never reached the mid-turn composer"
        harness.evidence.write(case, "10-transcript.txt", _capture(harness, claude_session))
        _stop_turn(harness, claude_session)

    def test_02_navigation_and_escape_deliver(
        self, harness: Harness, claude_session: ProviderSession
    ):
        case = "r15-claude-02-navigation"
        _start_long_turn(harness, claude_session)
        for name, events in {
            "arrows": [{"type": "key", "key": "Down"}, {"type": "key", "key": "Up"}],
            "escape": [{"type": "key", "key": "Escape"}],
        }.items():
            request, response = _post_events(
                harness, claude_session, events, payload_class="interactive"
            )
            harness.evidence.write_json(case, f"{name}-request.json", request)
            harness.evidence.write_json(case, f"{name}-response.json", response)
            assert response["outcome"] == "accepted", (name, response)
        harness.evidence.write(case, "10-transcript.txt", _capture(harness, claude_session))
        _stop_turn(harness, claude_session)

    def test_03_stale_identity_and_copy_mode_are_zero_byte_refusals(
        self, harness: Harness, claude_session: ProviderSession
    ):
        case = "r15-claude-03-stale-and-copy-mode"
        _start_long_turn(harness, claude_session)

        marker = f"R15CSTALE-{uuid.uuid4().hex[:8]}"
        tampered = {
            **_expected_identity(harness, claude_session),
            "terminal_generation": "00000000-0000-0000-0000-000000000000",
        }
        request, response = _post(
            harness,
            claude_session,
            {
                "events": [{"type": "text", "text": marker}],
                "payload_class": "interactive",
                "expected_identity": tampered,
            },
        )
        harness.evidence.write_json(case, "stale-request.json", request)
        harness.evidence.write_json(case, "stale-response.json", response)
        assert response["outcome"] == "refused", response
        assert response["reason_code"] in ("stale-generation", "identity-mismatch"), response
        assert marker not in _capture(harness, claude_session), "zero bytes were not zero"

        marker = f"R15CCOPY-{uuid.uuid4().hex[:8]}"
        harness.tmux.out("copy-mode", "-t", claude_session.pane_id)
        try:
            request, response = _post_events(
                harness,
                claude_session,
                [{"type": "text", "text": marker}],
                payload_class="interactive",
            )
            still_in_mode = harness.tmux.out(
                "display-message", "-p", "-t", claude_session.pane_id, "#{pane_in_mode}"
            )
        finally:
            harness.tmux.run("send-keys", "-t", claude_session.pane_id, "-X", "cancel", check=False)
        harness.evidence.write_json(case, "copy-mode-request.json", request)
        harness.evidence.write_json(case, "copy-mode-response.json", response)
        harness.evidence.note(case, f"pane_in_mode after the refused POST: {still_in_mode}")
        assert response["outcome"] == "refused", response
        assert response["reason_code"] == "copy-mode-active", response
        assert still_in_mode.strip() == "1", "the guard exited the operator's copy mode"
        assert marker not in _capture(harness, claude_session), "zero bytes were not zero"
        harness.evidence.write(case, "10-transcript.txt", _capture(harness, claude_session))
        _stop_turn(harness, claude_session)

    def test_04_killed_response_reconciles_exact_id_never_resend(
        self, harness: Harness, claude_session: ProviderSession
    ):
        case = "r15-claude-04-killed-response"
        _start_long_turn(harness, claude_session)
        marker = f"R15CKILL-{uuid.uuid4().hex[:8]}"
        control_id = f"ctl-{uuid.uuid4().hex[:10]}"
        body: Dict[str, Any] = {
            "control_id": control_id,
            "events": [{"type": "text", "text": marker}, {"type": "key", "key": "Enter"}],
            "payload_class": "interactive",
            "expected_identity": _expected_identity(harness, claude_session),
        }
        harness.evidence.write_json(case, "submit-killed-request.json", body)
        note = _post_killing_response(
            harness, f"/terminals/{claude_session.terminal_id}/control-input", body
        )
        harness.evidence.note(case, note)

        assert _await(
            lambda: _transcript_count(harness, claude_session, marker) >= 1,
            timeout=TURN_TIMEOUT,
            poll=2.0,
        ), "the killed-response interactive batch never reached the provider"

        state = _await_journal_terminal(harness, control_id)
        harness.evidence.note(
            case, f"journal reached terminal state {state!r} before the one reconcile"
        )
        reconcile = requests.get(f"{harness.server.url}/control-input/{control_id}", timeout=30)
        assert reconcile.status_code == 200
        outcome = reconcile.json()
        harness.evidence.write_json(case, "reconcile-response.json", outcome)
        assert outcome["outcome"] == "accepted", outcome

        count = _transcript_count(harness, claude_session, marker)
        harness.evidence.note(case, f"marker occurrences in transcript: {count}")
        assert count == 1, f"marker appears {count} times — a duplicate submission happened"
        harness.evidence.write(case, "10-transcript.txt", _capture(harness, claude_session))
        _stop_turn(harness, claude_session)

    def test_05_mid_turn_model_command_is_a_queued_command_limit(
        self, harness: Harness, claude_session: ProviderSession
    ):
        """r16 honest limit (pinned claude 2.1.220, live-proven provider
        behavior — never a CAO defect): during the active turn a declared
        interactive ``/model`` is accepted and **queued** in the native
        composer — no model/effort menu opens, the setting stays unchanged,
        and the active turn continues.  The control/journal evidence claims
        bytes delivered and a queued command only, never menu execution or
        cancellation.  If a future build opens a real menu here, this test
        fails loudly for re-pinning rather than silently passing."""
        case = "r16-claude-05-queued-command-limit"
        _start_long_turn(harness, claude_session)
        progress_at_open = _turn_progress(_capture(harness, claude_session))
        model_before = _claude_config_model()
        harness.evidence.note(
            case, "model key present in ~/.claude.json before drill: " f"{model_before is not None}"
        )

        # The command by its real supported interaction, declared interactive.
        request, response = _post_events(
            harness,
            claude_session,
            [{"type": "text", "text": "/model"}, {"type": "key", "key": "Enter"}],
            payload_class="interactive",
        )
        harness.evidence.write_json(case, "01-model-enter-request.json", request)
        harness.evidence.write_json(case, "02-model-enter-response.json", response)
        assert response["outcome"] == "accepted", response
        # The control/journal evidence claims bytes delivered only — a
        # queued command, never a menu action.
        harness.evidence.note(
            case,
            "control evidence claim: bytes delivered (accepted, per-event sent) — a "
            "queued command; no menu execution or cancellation is claimed anywhere",
        )

        # The pinned provider behavior: the command is VISIBLY QUEUED in
        # the composer, and no model/effort menu opens mid-turn.
        assert _await(
            lambda: "/model" in _bottom_rows(_capture(harness, claude_session), 8)
            and "queued" in _capture(harness, claude_session).lower(),
            timeout=30.0,
        ), "the /model command never appeared in the mid-turn composer queue"
        queued = _capture(harness, claude_session)
        harness.evidence.write(case, "10-command-visibly-queued.txt", queued)
        menu_opened_mid_turn = _await(
            lambda: _claude_menu_open(harness, claude_session), timeout=8.0, poll=0.25
        )
        harness.evidence.note(
            case,
            f"model/effort menu opened during the active turn: {menu_opened_mid_turn} "
            "(the pinned limit says it must not)",
        )
        assert not menu_opened_mid_turn, (
            "a model/effort menu opened mid-turn on claude — the r16 queued-command "
            "limit no longer holds; re-pin the provider behavior before landing"
        )

        # The active turn continues (provider-correct outcome) and settles.
        assert _await(
            lambda: _turn_progress(_capture(harness, claude_session)) > progress_at_open,
            timeout=90.0,
            poll=1.0,
        ), "the active turn did not continue after the command queued"
        harness.evidence.write(case, "20-turn-continued.txt", _capture(harness, claude_session))
        _wait_turn_done(harness, claude_session)
        settled = _capture(harness, claude_session)
        harness.evidence.write(case, "30-after-turn-completed.txt", settled)

        # Post-turn the queued command is processed like any queued input
        # (the palette may open now — ordinary post-turn processing, not a
        # mid-turn menu).  Close it idle-safe if so; assert nothing changed.
        if _claude_menu_open(harness, claude_session):
            harness.evidence.note(
                case,
                "post-turn the queued /model opened the selector (ordinary queued-input "
                "processing after the turn, not a mid-turn menu); Escape-closed idle",
            )
            _post_events(harness, claude_session, [{"type": "key", "key": "Escape"}])
            assert _await(
                lambda: not _claude_menu_open(harness, claude_session), timeout=8.0, poll=0.25
            ), "the post-turn selector did not close on Escape while idle"
            harness.evidence.write(
                case, "31-post-turn-selector-closed.txt", _capture(harness, claude_session)
            )

        model_after = _claude_config_model()
        harness.evidence.note(
            case, "model key present in ~/.claude.json after drill: " f"{model_after is not None}"
        )
        assert model_after == model_before, "the drill changed the persisted model setting"
        status = _bottom_rows(_capture(harness, claude_session), 6)
        assert "sonnet-5" in status, f"the status line no longer shows the pinned model:\n{status}"
        block = _identity_block(harness, claude_session, "claude_code")
        assert block["interactive_streaming"] == {"supported": True}
        harness.evidence.write_json(case, "40-identity-reread.json", block)
