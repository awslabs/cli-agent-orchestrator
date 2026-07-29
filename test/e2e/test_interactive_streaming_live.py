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
import os
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

    def test_01_capability_and_undeclared_gate(self, harness: Harness, kimi_session: ProviderSession):
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
        harness.evidence.write(case, "10-transcript-active-turn.txt", _capture(harness, kimi_session))
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
            request, response = _post_events(harness, kimi_session, events, payload_class="interactive")
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
        descriptor = os.open(
            str(lock_dir / f"pane-{pane[1:]}.lock"), os.O_CREAT | os.O_RDWR, 0o600
        )
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
                harness, kimi_session, [{"type": "text", "text": marker}], payload_class="interactive"
            )
            still_in_mode = harness.tmux.out(
                "display-message", "-p", "-t", kimi_session.pane_id, "#{pane_in_mode}"
            )
        finally:
            harness.tmux.run(
                "send-keys", "-t", kimi_session.pane_id, "-X", "cancel", check=False
            )
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
        harness.evidence.note(case, f"journal reached terminal state {state!r} before the one reconcile")
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
            request, response = _post_events(harness, claude_session, events, payload_class="interactive")
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
                harness, claude_session, [{"type": "text", "text": marker}], payload_class="interactive"
            )
            still_in_mode = harness.tmux.out(
                "display-message", "-p", "-t", claude_session.pane_id, "#{pane_in_mode}"
            )
        finally:
            harness.tmux.run(
                "send-keys", "-t", claude_session.pane_id, "-X", "cancel", check=False
            )
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
        harness.evidence.note(case, f"journal reached terminal state {state!r} before the one reconcile")
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
