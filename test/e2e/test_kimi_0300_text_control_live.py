"""cond-0198 — Kimi 0.30.0 exact-build text/control live acceptance.

Drives the native text/control path against a disposable managed native-TUI
session on the REAL installed Kimi 0.30.0 binary (the operator's build after
the auto-update; no version shim), under the existing identity-bound arbiter
and every r15/r16 safeguard (lease, identity, copy-mode, deadline, journal,
raw-WS).  The per-terminal registry pins added for this exact build —
`SUPPORTED_VERSIONS`/`PINNED_VERSIONS` entry, `_PROVEN_COMPOSER_NEWLINE`
and `_PROVEN_STEER_CHORDS` entries — are the claim under test: the build
must accept and execute the pinned text flow with truthful outcomes.

* capability: the per-terminal, build-exact block advertises
  operator_message, interactive_streaming, and the C-s steer chord — and
  NOT the image block (image authority stays pinned to 0.29.2 alone).
* text/control: v1 text+Enter accepted and typed; a multiline operator
  message accepted through the proven C-j composer plan; a declared
  interactive batch accepted mid-turn while the same-shaped undeclared
  batch stays readiness-gated; declared C-s mid-turn consumed AND acted
  on — proven by the unique exact ACK line appearing as its own fresh
  provider-output row (never the instruction row, an echo, or pre-steer
  content, and no second prompt carrying the token).
* image fail-closed: a PNG upload on this build is refused
  provider-unsupported — no image delivery is advertised or delivered.

Evidence (sanitized request/response JSON + transcript captures) is written
under ``$CAO_LANE_C_EVIDENCE_DIR`` or a per-run scratch dir.

Run with:

    uv run pytest -m e2e test/e2e/test_kimi_0300_text_control_live.py -v
"""

from __future__ import annotations

import os
import re
import shutil
import struct
import tempfile
import time
import uuid
import zlib
from pathlib import Path
from test.e2e.test_interactive_streaming_live import (
    EVIDENCE_ENV,
    _bottom_rows,
    _identity_block,
    _start_long_turn,
    _stop_turn,
    _turn_progress,
)
from test.e2e.test_native_tui_provider_acceptance import (
    Evidence,
    Harness,
    ProviderSession,
    _await,
    _build_kimi_provider_home_shim,
    _capture,
    _harvest_email_tokens,
    _kill_session,
    _launch_provider_session,
    _post,
    _post_events,
    _turn_active,
)
from test.e2e.test_operator_message_live import (
    EFFORT_PROVIDER_DEFAULT,
    KIMI_MODEL,
    LANE_C_PROFILE,
    TURN_TIMEOUT,
    _expected_identity,
    _harvest_account_display_names,
    _submit,
    _wait_turn_done,
)
from test.fixtures.cao_server import _pick_free_port, _start_cao_server
from test.fixtures.tmux_server import TmuxServer, isolated_tmux_server
from typing import Any, Dict, Iterator

import pytest
import requests

pytestmark = pytest.mark.e2e

KIMI_0300_PIN = "0.30.0"

_SCRUB_EXACT = {
    "CAO_TERMINAL_ID",
    "CAO_ALLOWED_HOSTS",
    "CAO_WS_ALLOWED_CLIENTS",
    "KIMI_MODEL_THINKING_EFFORT",
}
_SCRUB_PREFIXES = ("CAO_CONDUCTOR_", "CAO_WORKFLOW_")


def _png_fixture(width: int = 120, height: int = 80) -> bytes:
    """A real 120×80 PNG: left half red, right half blue (the r3 fixture)."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b""
    for _ in range(height):
        raw += b"\x00" + b"\xff\x00\x00" * (width // 2) + b"\x00\x00\xff" * (width - width // 2)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
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
        pytest.skip("cond-0198 acceptance needs the operator's real $HOME (provider auth)")
    home_path = Path(real_home)

    state_root = Path(tmp_path_factory.mktemp("cao_state_0300"))
    scratch = Path(tmp_path_factory.mktemp("cao_scratch_0300"))
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
def kimi_0300_session(harness: Harness) -> Iterator[ProviderSession]:
    session = _launch_provider_session(
        harness,
        provider="kimi_cli",
        binary="kimi",
        pin=KIMI_0300_PIN,
        expected_model=KIMI_MODEL,
        expected_effort=EFFORT_PROVIDER_DEFAULT,
        tag="kimi-0300",
        agent_profile=LANE_C_PROFILE,
    )
    try:
        yield session
    finally:
        _kill_session(harness, session)


def _production_progress(transcript: str) -> int:
    """Distinct counting-sequence values produced so far.

    Counts line-start integers (optionally trailed by punctuation) in the
    conversation region above the status/queue zone, so the context
    counter and status text can never inflate it.  The prompt itself
    contributes no line-start integers, so ≥3 means production began —
    robust to the model writing ``17`` or ``17.`` or ``17)``."""
    head = "\n".join(transcript.splitlines()[:-10])
    return len(set(re.findall(r"(?m)^\s*(\d{1,3})[\s.,)]*\s*$", head)))


def _start_long_count(harness: Harness, session: ProviderSession, target: int = 500) -> None:
    """Start a counting turn long enough to stay mid-production through the
    whole steer drill (0.30.0 counts fast; 120 finished before the steer
    landed in the first run — a timing artifact, not a provider failure)."""
    _wait_turn_done(harness, session)
    _post(
        harness,
        session,
        {
            "text": (
                f"Count from 1 to {target}, one number per line, thinking briefly "
                "between lines. Work steadily and do not stop early."
            ),
            "enter": True,
            "expected_identity": _expected_identity(harness, session),
        },
    )
    assert _await(
        lambda: _turn_active(harness, session), timeout=60.0, poll=0.5
    ), "the long counting turn never became observably active"


def _ack_rows(transcript: str, ack: str) -> list:
    """Fresh provider-output rows proving the steer effect (Sol r17).

    Only the exact provider-output origin counts: kimi renders its answers
    with the ``● `` response bullet, so the proof row is exactly
    ``● {ack}``.  A BARE ``{ack}`` continuation row is NOT accepted — the
    queued instruction can wrap so the ACK token lands alone on a
    continuation row, and that echo shape must never satisfy the
    predicate.  The ✨-marked instruction row and any wrapped echo carry
    the ACK inline or bare, never bulleted with exact content.
    """
    return [row.strip() for row in transcript.splitlines() if row.strip() == f"● {ack}"]


class TestKimi0300TextControl:
    """cond-0198: the exact 0.30.0 build accepts and executes the pinned
    text/control flow with truthful outcomes — image stays 0.29.2-only."""

    def test_01_capability_blocks_build_exact(
        self, harness: Harness, kimi_0300_session: ProviderSession
    ):
        case = "kimi-0300-01-capability"
        block = _identity_block(harness, kimi_0300_session, "kimi_cli")
        harness.evidence.write_json(case, "identity-provider-controls.json", block)
        assert block["operator_message"] == {
            "supported": True,
            "max_text_bytes": 8192,
            "multiline": True,
            "max_attachments": 4,
        }, block
        assert block["interactive_streaming"] == {"supported": True}, block
        assert block["steer_chords"] == ["C-s"], block
        assert "image" not in block, f"image authority must stay 0.29.2-only: {block}"
        harness.evidence.note(
            case,
            "0.30.0 advertises operator_message + interactive_streaming + C-s; "
            "no image block (image authority pinned to 0.29.2 only)",
        )

    def test_02_v1_text_and_enter_delivers(
        self, harness: Harness, kimi_0300_session: ProviderSession
    ):
        case = "kimi-0300-02-v1-text"
        _wait_turn_done(harness, kimi_0300_session)
        marker = f"V1TEXT-{uuid.uuid4().hex[:8]}"
        request, response = _post(
            harness,
            kimi_0300_session,
            {
                "text": f"Reply with exactly the marker {marker} and nothing else.",
                "enter": True,
                "expected_identity": _expected_identity(harness, kimi_0300_session),
            },
        )
        harness.evidence.write_json(case, "text-request.json", request)
        harness.evidence.write_json(case, "text-response.json", response)
        assert response["outcome"] == "accepted", response
        assert _await(
            lambda: marker in _capture(harness, kimi_0300_session), timeout=TURN_TIMEOUT, poll=2.0
        ), "the pinned v1 text flow did not reach the provider transcript"
        harness.evidence.write(case, "10-transcript.txt", _capture(harness, kimi_0300_session))
        _wait_turn_done(harness, kimi_0300_session)

    def test_03_multiline_operator_message_via_proven_plan(
        self, harness: Harness, kimi_0300_session: ProviderSession
    ):
        case = "kimi-0300-03-multiline-operator-message"
        _wait_turn_done(harness, kimi_0300_session)
        marker = f"MULTI-{uuid.uuid4().hex[:6]}"
        line_one = f"{marker}-line-one"
        line_two = f"{marker}-line-two"
        outcome = _submit(
            harness,
            kimi_0300_session,
            case,
            text=(
                f"Reply with the two markers {line_one} and {line_two}, one per line.\n"
                f"{line_two} (this second line rides the proven C-j composer plan)"
            ),
        )
        assert outcome["outcome"] == "accepted", outcome
        assert _await(
            lambda: line_one in _capture(harness, kimi_0300_session)
            and line_two in _capture(harness, kimi_0300_session),
            timeout=TURN_TIMEOUT,
            poll=2.0,
        ), "the multiline operator message did not reach the transcript on 0.30.0"
        harness.evidence.write(case, "10-transcript.txt", _capture(harness, kimi_0300_session))
        _wait_turn_done(harness, kimi_0300_session)

    def test_04_interactive_bypass_mid_turn_with_fence(
        self, harness: Harness, kimi_0300_session: ProviderSession
    ):
        case = "kimi-0300-04-interactive-bypass"
        _start_long_turn(harness, kimi_0300_session)
        # The inheritance fence: undeclared stays readiness-gated mid-turn.
        request, response = _post_events(
            harness, kimi_0300_session, [{"type": "text", "text": "automation prose"}]
        )
        harness.evidence.write_json(case, "undeclared-request.json", request)
        harness.evidence.write_json(case, "undeclared-response.json", response)
        assert response["outcome"] == "refused", response
        assert response["reason_code"] == "pane-busy", response

        marker = f"IACT-{uuid.uuid4().hex[:8]}"
        request, response = _post_events(
            harness,
            kimi_0300_session,
            [{"type": "text", "text": marker}],
            payload_class="interactive",
        )
        harness.evidence.write_json(case, "interactive-request.json", request)
        harness.evidence.write_json(case, "interactive-response.json", response)
        assert response["outcome"] == "accepted", response
        assert response.get("request_schema_version") == 4, response
        assert _await(
            lambda: marker in _capture(harness, kimi_0300_session), timeout=30.0
        ), "the declared interactive text never reached the mid-turn composer on 0.30.0"
        harness.evidence.write(case, "10-transcript.txt", _capture(harness, kimi_0300_session))
        _stop_turn(harness, kimi_0300_session)

    def test_05_declared_c_s_steer_effect_in_fresh_output(
        self, harness: Harness, kimi_0300_session: ProviderSession
    ):
        """cond-0198 (steers 134/136; predicate hardened per Sol r17): the
        declared C-s steer on 0.30.0 must prove the requested EFFECT — the
        provider consumes the queued steer and acts on it, shown by the
        unique exact ACK line appearing as its own fresh PROVIDER-BULLETED
        output row (``● {ack}``).  A bare ACK continuation row is rejected:
        the queued instruction can wrap its token onto one, and neither it,
        the ✨ instruction row, nor pre-steer content may ever satisfy the
        predicate; no second prompt carries the token, so causality is the
        steer alone."""
        case = "kimi-0300-05-steer-effect"
        _start_long_count(harness, kimi_0300_session, target=500)
        assert _await(
            lambda: _production_progress(_capture(harness, kimi_0300_session)) >= 3,
            timeout=180.0,
            poll=1.0,
        ), "the counting turn never started producing numbers"
        marker = f"STEERME-{uuid.uuid4().hex[:8]}"
        ack = f"STEER-ACK-{uuid.uuid4().hex[:10]}"
        instruction = f"{marker}: reply with exactly the line {ack} and nothing else."
        request, response = _post_events(
            harness,
            kimi_0300_session,
            [{"type": "text", "text": instruction}],
            payload_class="interactive",
        )
        harness.evidence.write_json(case, "01-queue-response.json", response)
        assert response["outcome"] == "accepted", response
        assert _await(
            lambda: marker in _bottom_rows(_capture(harness, kimi_0300_session), 10), timeout=30.0
        ), "the instruction never appeared in the mid-turn queue"
        pre_steer = _capture(harness, kimi_0300_session)
        harness.evidence.write(case, "10-queued.txt", pre_steer)

        assert not _ack_rows(pre_steer, ack), "the exact ACK line already existed before the steer"
        # The steer must land MID-TURN: if production already ended, the
        # drill proves nothing about a mid-turn steer (the first run's
        # timing artifact).  Fail the drill, not the provider, in that case.
        assert _turn_active(harness, kimi_0300_session), (
            "the counting turn ended before C-s — the drill's timing is invalid, "
            "not a provider result; lengthen the turn"
        )

        request, response = _post_events(
            harness,
            kimi_0300_session,
            [{"type": "chord", "chord": "C-s"}],
            payload_class="interactive",
        )
        harness.evidence.write_json(case, "20-steer-response.json", response)
        assert response["outcome"] == "accepted", response

        delivered = _await(
            lambda: _ack_rows(_capture(harness, kimi_0300_session), ack),
            timeout=300.0,
            poll=2.0,
        )
        acted = _capture(harness, kimi_0300_session)
        harness.evidence.write(case, "30-provider-acted.txt", acted)
        effect_rows = _ack_rows(acted, ack)
        if not effect_rows:
            harness.evidence.note(
                case,
                "EFFECT NOT DELIVERED on 0.30.0: the exact ACK line never appeared as a "
                "fresh provider-output row; settled capture in 30-provider-acted.txt "
                "for adjudication",
            )
        assert (
            effect_rows
        ), "the exact ACK line never appeared as a fresh provider-output row on 0.30.0"
        assert not any(
            row in pre_steer for row in effect_rows
        ), "the ACK rows predate the steer — echo, not effect"
        harness.evidence.note(
            case,
            f"steer effect proven on 0.30.0: the exact ACK line {ack!r} appears as its "
            f"own fresh PROVIDER-BULLETED output row ({len(effect_rows)}x, `● {ack}`); "
            "a bare ACK continuation row, the instruction row, a wrapped "
            "queue/composer echo, or pre-steer content can never satisfy the "
            "predicate, so the provider consumed the queued C-s steer and acted on it",
        )
        _stop_turn(harness, kimi_0300_session)

    def test_06_image_upload_is_refused_provider_unsupported(
        self, harness: Harness, kimi_0300_session: ProviderSession
    ):
        case = "kimi-0300-06-image-refused"
        response = requests.post(
            f"{harness.server.url}/terminals/{kimi_0300_session.terminal_id}/attachments",
            files={"file": ("stripe.png", _png_fixture(), "image/png")},
            timeout=30,
        )
        harness.evidence.write_json(
            case,
            "upload-response.json",
            {"status_code": response.status_code, "body": response.json()},
        )
        assert response.status_code == 422, response.text
        body = response.json()
        assert body["outcome"] == "refused", body
        assert body["reason_code"] == "provider-unsupported", body
        harness.evidence.note(
            case,
            "image delivery authority stays pinned to 0.29.2: the upload on 0.30.0 "
            "is refused provider-unsupported with no staged file",
        )
