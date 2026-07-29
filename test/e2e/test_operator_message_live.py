"""§10.6 Lane C installed live-provider acceptance.

Drives the real operator-message path end-to-end against disposable managed
native-TUI sessions on the pinned provider builds (kimi 0.29.2, claude
2.1.220), reusing the Lane A acceptance harness (private tmux socket,
temp CAO_STATE_ROOT, real $HOME for provider auth):

* kimi: a staged PNG submitted via POST /terminals/{id}/operator-message —
  the staged absolute path reaches the composer inside the pinned
  ReadMediaFile directive template, the provider invokes ReadMediaFile on
  it, and the model reports the fixture's known halves (red left, blue
  right).  The upstream capability was proven at round 3; what this proves
  is the Lane C *server path* (upload → token substitution → typed send).
* kimi: a >512-byte text-only operator message delivered via the
  build-proven composer plan.
* at-most-once: an identical same-id re-POST replays the journaled answer
  with zero new bytes (the marker appears exactly once in the transcript),
  and a divergent same-id POST is request-rebound.
* killed response (§10.6, r1): the submit POST is sent over a raw socket
  that closes without reading — the response is provably lost mid-submit
  while the server completes the write — then one exact-id GET reconciles
  to the journaled accepted answer and the transcript proves exactly one
  provider write.
* claude: a staged PNG reference reaches the composer and the provider
  can read the staged file.
* kimi: a non-PNG upload is refused 422 attachment-type-unsupported.

Evidence (sanitized request/response JSON + transcript captures) is written
under ``$CAO_LANE_C_EVIDENCE_DIR`` or a per-run scratch dir.

Run with:

    uv run pytest -m e2e test/e2e/test_operator_message_live.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import tempfile
import time
import uuid
import zlib
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
    _turn_active,
)
from test.fixtures.cao_server import _pick_free_port, _start_cao_server
from test.fixtures.tmux_server import TmuxServer, isolated_tmux_server
from typing import Dict, Iterator, List

import pytest
import requests

pytestmark = pytest.mark.e2e

KIMI_PIN = "0.29.2"
CLAUDE_PIN = "2.1.220"
KIMI_MODEL = "kimi-code/kimi-for-coding"
EFFORT_PROVIDER_DEFAULT = "provider-default"
CLAUDE_MODEL_ALIAS = "sonnet"

# The Lane C acceptance profile: no MCP servers (they stall the boot gate
# on the pane's bounded PATH) and NO "never use tools" line — the kimi
# image proof requires the provider to invoke its own ReadMediaFile tool.
LANE_C_PROFILE = "lanec-acceptance"
_LANE_C_PROFILE_DOC = """---
name: lanec-acceptance
description: Disposable Lane C 10.6 acceptance profile (no MCP servers)
---

You are a disposable acceptance-test agent.  Keep every reply as short as
possible — one short sentence when you can.
"""

EVIDENCE_ENV = "CAO_LANE_C_EVIDENCE_DIR"


def _harvest_account_display_names(home_path: Path) -> List[str]:
    """Account display-name tokens to redact from provider welcome banners,
    read but never printed (the same harvest discipline as the email
    tokens).  Covers the full display name and its first token, which is
    the form the Claude banner renders."""
    names: List[str] = []
    try:
        data = json.loads(
            (home_path / ".claude.json").read_text(encoding="utf-8", errors="replace")
        )
    except (OSError, json.JSONDecodeError):
        return []
    account = data.get("oauthAccount")
    if isinstance(account, dict):
        for key in ("displayName", "organizationName"):
            value = account.get(key)
            if isinstance(value, str) and value.strip():
                names.append(value.strip())
                parts = value.strip().split()
                if parts:
                    names.append(parts[0])
    return sorted(set(names), key=len, reverse=True)


_SCRUB_EXACT = {
    "CAO_TERMINAL_ID",
    "CAO_ALLOWED_HOSTS",
    "CAO_WS_ALLOWED_CLIENTS",
    "KIMI_MODEL_THINKING_EFFORT",
}
_SCRUB_PREFIXES = ("CAO_CONDUCTOR_", "CAO_WORKFLOW_")

# How long to wait for a model turn that must read an image and answer.
TURN_TIMEOUT = 300.0
# Post-Enter kimi dispatch grace (pinned 5.0s) plus margin.
GRACE_SLEEP = 5.8

IDENTITY_FIELDS = (
    "terminal_id",
    "terminal_incarnation",
    "terminal_generation",
    "pane_birth_id",
    "provider_process_id",
    "provider",
    "native_session_id",
    "execution_mode",
    "session_name",
)


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


def _jpeg_smuggle() -> bytes:
    """Minimal JPEG-shaped bytes (SOI + SOF0) for the non-PNG refusal case."""
    return (
        b"\xff\xd8"
        + b"\xff\xe0"
        + struct.pack(">H", 4)
        + b"\x00\x00"
        + b"\xff\xc0"
        + struct.pack(">H", 9)
        + b"\x08"
        + struct.pack(">H", 48)
        + struct.pack(">H", 64)
        + b"\x00"
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
        pytest.skip("§10.6 acceptance needs the operator's real $HOME (provider auth)")
    home_path = Path(real_home)

    state_root = Path(tmp_path_factory.mktemp("cao_state_lanec"))
    scratch = Path(tmp_path_factory.mktemp("cao_scratch_lanec"))
    server_bookkeeping = scratch / "server-home"

    agent_store = state_root / "agent-store"
    agent_store.mkdir(parents=True, exist_ok=True)
    (agent_store / f"{LANE_C_PROFILE}.md").write_text(_LANE_C_PROFILE_DOC, encoding="utf-8")

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
        tag="lanec-kimi",
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
        tag="lanec-claude",
        agent_profile=LANE_C_PROFILE,
    )
    try:
        yield session
    finally:
        _kill_session(harness, session)


# ---------------------------------------------------------------------------
# Operator-message helpers
# ---------------------------------------------------------------------------


def _expected_identity(harness: Harness, session: ProviderSession) -> Dict[str, object]:
    identity = _control_identity(harness, session)
    return {field: identity.get(field) for field in IDENTITY_FIELDS}


def _upload(
    harness: Harness,
    session: ProviderSession,
    case: str,
    *,
    content: bytes,
    filename: str,
    mime: str,
) -> Dict[str, object]:
    response = requests.post(
        f"{harness.server.url}/terminals/{session.terminal_id}/attachments",
        files={"file": (filename, content, mime)},
        timeout=30,
    )
    harness.evidence.write_json(
        case,
        f"upload-{filename}-response.json",
        {"status_code": response.status_code, "body": response.json()},
    )
    assert response.status_code == 201, f"upload failed: {response.status_code} {response.text}"
    return response.json()["attachment"]


def _submit(
    harness: Harness,
    session: ProviderSession,
    case: str,
    *,
    text: str,
    attachments: list[str] | None = None,
    token_map: Dict[str, str] | None = None,
    operation_id: str | None = None,
    name: str = "submit",
) -> Dict[str, object]:
    body = {
        "operation_id": operation_id or str(uuid.uuid4()),
        "text": text,
        "attachments": attachments or [],
        "token_map": token_map or {},
        "expected_identity": _expected_identity(harness, session),
    }
    response = requests.post(
        f"{harness.server.url}/terminals/{session.terminal_id}/operator-message",
        json=body,
        timeout=90,
    )
    harness.evidence.write_json(
        case,
        f"{name}-request.json",
        {k: (v if k != "expected_identity" else "<9 fields bound>") for k, v in body.items()},
    )
    harness.evidence.write_json(
        case,
        f"{name}-response.json",
        {"status_code": response.status_code, "body": response.json()},
    )
    assert response.status_code == 200, f"submit failed: {response.status_code} {response.text}"
    return response.json()


def _wait_turn_done(
    harness: Harness, session: ProviderSession, timeout: float = TURN_TIMEOUT
) -> None:
    assert _await(
        lambda: not _turn_active(harness, session), timeout=timeout, poll=1.0
    ), "the provider turn never settled to an input-ready composer"
    time.sleep(GRACE_SLEEP)


def _marker_count(harness: Harness, session: ProviderSession, marker: str) -> int:
    screen = harness.tmux.out("capture-pane", "-p", "-S-400", "-t", session.pane_id)
    return screen.count(marker)


def _post_killing_response(harness: Harness, path: str, body: Dict[str, object]) -> str:
    """POST over a raw socket and close without reading: the response is
    provably lost mid-submit while the server completes the write.

    ``HTTPConnection.request`` blocks until every byte is written to the
    socket, so the request is fully delivered; closing before any read
    means the client can never see the answer (the server's response write
    fails harmlessly after the submit completed).  This is the §10.6
    killed-response drill — not a replay, not a short read timeout.
    """
    import http.client
    import json as jsonlib
    from urllib.parse import urlparse

    url = urlparse(harness.server.url)
    payload = jsonlib.dumps(body).encode()
    connection = http.client.HTTPConnection(url.hostname, url.port, timeout=15)
    connection.request(
        "POST",
        path,
        body=payload,
        headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
    )
    connection.close()
    return "connection closed without reading the response (response killed mid-submit)"


class TestKimiOperatorMessage:
    def test_01_capability_blocks_advertised(self, harness: Harness, kimi_session: ProviderSession):
        identity = _control_identity(harness, kimi_session)
        block = identity["control_input"]["provider_controls"]["kimi_cli"]
        assert block["operator_message"] == {
            "supported": True,
            "max_text_bytes": 8192,
            "multiline": True,
            "max_attachments": 4,
        }
        assert block["image"]["formats"] == ["png"]
        assert block["image"]["mechanism"] == "staged-path-text"
        assert "{path}" in block["image"]["reference_template"]

    def test_02_non_png_upload_refused(self, harness: Harness, kimi_session: ProviderSession):
        response = requests.post(
            f"{harness.server.url}/terminals/{kimi_session.terminal_id}/attachments",
            files={"file": ("photo.jpg", _jpeg_smuggle(), "image/jpeg")},
            timeout=30,
        )
        harness.evidence.write_json(
            "kimi-02-non-png",
            "upload-response.json",
            {"status_code": response.status_code, "body": response.json()},
        )
        assert response.status_code == 422
        body = response.json()
        assert body["outcome"] == "refused"
        assert body["reason_code"] == "attachment-type-unsupported"
        assert body["attachment"]["state"] == "failed"

    def test_03_staged_png_reaches_readmediafile(
        self, harness: Harness, kimi_session: ProviderSession
    ):
        case = "kimi-03-staged-png"
        # The admission task's turn must settle before a submit can pass the
        # readiness gate.
        _wait_turn_done(harness, kimi_session)
        attachment = _upload(
            harness,
            kimi_session,
            case,
            content=_png_fixture(),
            filename="stripe-fixture.png",
            mime="image/png",
        )
        assert attachment["state"] == "ready"
        assert (attachment["width"], attachment["height"]) == (120, 80)

        outcome = _submit(
            harness,
            kimi_session,
            case,
            text=(
                "[Image #1] This image has one solid color on its left half and "
                "another on its right half. Name the two colors in one short sentence."
            ),
            attachments=[attachment["attachment_id"]],
            token_map={"1": attachment["attachment_id"]},
        )
        assert outcome["outcome"] == "accepted", outcome

        # The staged absolute path reference reached the composer, inside the
        # pinned ReadMediaFile directive template.
        staged_marker = f"attachments/{kimi_session.terminal_id}/{attachment['attachment_id']}.png"
        assert _await(
            lambda: staged_marker in _capture(harness, kimi_session), timeout=30.0
        ), "the staged-path reference never appeared in the transcript"
        assert _await(
            lambda: "ReadMediaFile" in _capture(harness, kimi_session), timeout=30.0
        ), "the directive template did not reach the composer"
        harness.evidence.write(
            case, "10-transcript-after-submit.txt", _capture(harness, kimi_session)
        )

        # The provider's own ReadMediaFile reads the staged file and the
        # model reports the fixture's known halves.
        assert _await(
            lambda: "ReadMediaFile"
            in harness.tmux.out("capture-pane", "-p", "-S-400", "-t", kimi_session.pane_id),
            timeout=TURN_TIMEOUT,
            poll=2.0,
        ), "the provider never invoked ReadMediaFile"
        assert _await(
            lambda: "red"
            in harness.tmux.out("capture-pane", "-p", "-S-400", "-t", kimi_session.pane_id).lower()
            and "blue"
            in harness.tmux.out("capture-pane", "-p", "-S-400", "-t", kimi_session.pane_id).lower(),
            timeout=TURN_TIMEOUT,
            poll=2.0,
        ), "the model never reported the fixture's red/blue halves"
        harness.evidence.write(
            case,
            "20-transcript-after-answer.txt",
            harness.tmux.out("capture-pane", "-p", "-S-400", "-t", kimi_session.pane_id),
        )
        _wait_turn_done(harness, kimi_session)

    def test_04_long_text_via_proven_plan(self, harness: Harness, kimi_session: ProviderSession):
        case = "kimi-04-long-text"
        _wait_turn_done(harness, kimi_session)
        marker = f"MARKER-{uuid.uuid4().hex[:8]}"
        text = f"Reply with exactly the marker {marker} and nothing else. " + (
            "This operator message deliberately exceeds the 512-byte "
            "control-input limit to prove the operator-message path. " * 8
        )
        assert len(text.encode()) > 512
        outcome = _submit(harness, kimi_session, case, text=text)
        assert outcome["outcome"] == "accepted", outcome
        assert _await(
            lambda: marker
            in harness.tmux.out("capture-pane", "-p", "-S-400", "-t", kimi_session.pane_id),
            timeout=TURN_TIMEOUT,
            poll=2.0,
        ), "the >512-byte message never reached the transcript"
        harness.evidence.write(
            case,
            "10-transcript.txt",
            harness.tmux.out("capture-pane", "-p", "-S-400", "-t", kimi_session.pane_id),
        )
        _wait_turn_done(harness, kimi_session)

    def test_05_at_most_once_replay_and_rebound(
        self, harness: Harness, kimi_session: ProviderSession
    ):
        case = "kimi-05-at-most-once"
        _wait_turn_done(harness, kimi_session)
        marker = f"ONCE-{uuid.uuid4().hex[:8]}"
        operation_id = str(uuid.uuid4())
        outcome = _submit(
            harness,
            kimi_session,
            case,
            text=f"Reply with exactly the marker {marker} and nothing else.",
            operation_id=operation_id,
            name="submit-original",
        )
        assert outcome["outcome"] == "accepted", outcome

        # Identical re-POST: the journaled answer replays with zero new I/O.
        replay = _submit(
            harness,
            kimi_session,
            case,
            text=f"Reply with exactly the marker {marker} and nothing else.",
            operation_id=operation_id,
            name="submit-replay",
        )
        assert replay["outcome"] == "accepted", replay
        assert replay["replayed"] is True, replay

        # Divergent same-id POST: request-rebound, also zero new bytes.
        rebound = _submit(
            harness,
            kimi_session,
            case,
            text=f"Reply with exactly the marker {marker} and nothing else. Diverged.",
            operation_id=operation_id,
            name="submit-rebound",
        )
        assert rebound["outcome"] == "refused", rebound
        assert rebound["reason_code"] == "request-rebound", rebound

        # The reconcile route answers the same journaled record.
        reconcile = requests.get(
            f"{harness.server.url}/operator-message/{operation_id}", timeout=30
        )
        assert reconcile.status_code == 200
        assert reconcile.json()["outcome"] == "accepted"
        harness.evidence.write_json(case, "reconcile-response.json", reconcile.json())

        # Exactly one submission of the marker ever reached the provider.
        assert _await(lambda: _marker_count(harness, kimi_session, marker) >= 1, timeout=60.0)
        count = _marker_count(harness, kimi_session, marker)
        harness.evidence.note(case, f"marker occurrences in transcript: {count}")
        assert count == 1, f"marker appears {count} times — a duplicate submission happened"
        harness.evidence.write(
            case,
            "10-transcript.txt",
            harness.tmux.out("capture-pane", "-p", "-S-400", "-t", kimi_session.pane_id),
        )
        _wait_turn_done(harness, kimi_session)

    def test_06_killed_response_mid_submit_reconciles_to_exactly_one_write(
        self, harness: Harness, kimi_session: ProviderSession
    ):
        """§10.6 r1: response killed mid-submit → exact-id reconcile →
        exactly one provider write (never an ordinary replay)."""
        case = "kimi-06-killed-response"
        _wait_turn_done(harness, kimi_session)
        marker = f"KILLED-{uuid.uuid4().hex[:8]}"
        operation_id = str(uuid.uuid4())
        body: Dict[str, object] = {
            "operation_id": operation_id,
            "text": f"Reply with exactly the marker {marker} and nothing else.",
            "attachments": [],
            "token_map": {},
            "expected_identity": _expected_identity(harness, kimi_session),
        }
        harness.evidence.write_json(
            case,
            "submit-killed-request.json",
            {k: (v if k != "expected_identity" else "<9 fields bound>") for k, v in body.items()},
        )

        # The kill: the request is fully written, the connection closes
        # before any read — the client provably never saw the answer.
        note = _post_killing_response(
            harness, f"/terminals/{kimi_session.terminal_id}/operator-message", body
        )
        harness.evidence.note(case, note)

        # The submit continued server-side: the write reached the provider
        # even though the response died.
        assert _await(
            lambda: _marker_count(harness, kimi_session, marker) >= 1,
            timeout=TURN_TIMEOUT,
            poll=2.0,
        ), "the killed-response submit never reached the provider"

        # Exactly one exact-id reconcile: the journaled accepted answer.
        reconcile = requests.get(
            f"{harness.server.url}/operator-message/{operation_id}", timeout=30
        )
        assert reconcile.status_code == 200
        outcome = reconcile.json()
        harness.evidence.write_json(case, "reconcile-response.json", outcome)
        assert outcome["outcome"] == "accepted", outcome

        # The marker count proves exactly one provider write happened.
        count = _marker_count(harness, kimi_session, marker)
        harness.evidence.note(case, f"marker occurrences in transcript: {count}")
        assert count == 1, f"marker appears {count} times — a duplicate submission happened"
        harness.evidence.write(
            case,
            "10-transcript.txt",
            harness.tmux.out("capture-pane", "-p", "-S-400", "-t", kimi_session.pane_id),
        )
        _wait_turn_done(harness, kimi_session)


class TestClaudeOperatorMessage:
    def test_01_staged_png_reference_readable(
        self, harness: Harness, claude_session: ProviderSession
    ):
        case = "claude-01-staged-png"
        # The admission task's turn must settle before a submit can pass the
        # readiness gate.
        _wait_turn_done(harness, claude_session)
        attachment = _upload(
            harness,
            claude_session,
            case,
            content=_png_fixture(),
            filename="stripe-fixture.png",
            mime="image/png",
        )
        assert attachment["state"] == "ready"

        outcome = _submit(
            harness,
            claude_session,
            case,
            text=(
                "Read the image file at [Image #1] and tell me the two colors "
                "in it, left half and right half, in one short sentence."
            ),
            attachments=[attachment["attachment_id"]],
            token_map={"1": attachment["attachment_id"]},
        )
        assert outcome["outcome"] == "accepted", outcome

        # The bare staged path (claude's documented reference form) reached
        # the composer.
        staged_marker = (
            f"attachments/{claude_session.terminal_id}/{attachment['attachment_id']}.png"
        )
        assert _await(
            lambda: staged_marker in _capture(harness, claude_session), timeout=30.0
        ), "the staged-path reference never appeared in claude's transcript"
        # The provider can read the staged file: the fixture's colors come back.
        assert _await(
            lambda: "red"
            in harness.tmux.out(
                "capture-pane", "-p", "-S-400", "-t", claude_session.pane_id
            ).lower()
            and "blue"
            in harness.tmux.out(
                "capture-pane", "-p", "-S-400", "-t", claude_session.pane_id
            ).lower(),
            timeout=TURN_TIMEOUT,
            poll=2.0,
        ), "claude never reported the fixture's red/blue halves"
        harness.evidence.write(
            case,
            "20-transcript-after-answer.txt",
            harness.tmux.out("capture-pane", "-p", "-S-400", "-t", claude_session.pane_id),
        )
        _wait_turn_done(harness, claude_session)
