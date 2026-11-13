"""The run view's API: ``steps[].result`` on the inspection, and the screen and transcript routes.

Covers, against a real temp journal:

- ``GET /workflows/runs/{run_id}`` returns ``steps[].result`` parsed from the step row's
  ``result_json`` envelope, ``null`` for a row without one and for a corrupt envelope;
- ``GET .../steps/{step_id}/screen``: 200 with a redacted, bounded plain-text capture taken
  read-only through ``get_history``; 400 for a malformed key; 404 for an unknown run or step,
  no terminal recorded, or a recorded id that is not a terminal id; 410 once the terminal
  has ended;
- ``GET .../steps/{step_id}/transcript``: 200 with the redacted tail of the ``.scrollback``
  file from a line start; a secret straddling the 256 KiB edge is not returned in part; 400,
  404 (including symlinks and directories at the path);
- neither route builds a path from the request: the terminal id is the journal's;
- both routes carry the read-or-better scope gate and resolve to their own handlers.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List

import pytest
from starlette.routing import Match

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.backends import TerminalNotFoundError
from cli_agent_orchestrator.constants import (
    WORKFLOW_STEP_SCREEN_MAX_BYTES,
    WORKFLOW_STEP_SCREEN_MAX_LINES,
    WORKFLOW_STEP_TRANSCRIPT_MARGIN_BYTES,
    WORKFLOW_STEP_TRANSCRIPT_MAX_BYTES,
)
from cli_agent_orchestrator.models.workflow import WorkflowSpec, WorkflowStep
from cli_agent_orchestrator.security import auth
from cli_agent_orchestrator.services import workflow_journal, workflow_service, workflow_step_view
from cli_agent_orchestrator.services.step_result import build_envelope, serialise_envelope

_SPEC = WorkflowSpec(
    name="nightly-check",
    steps=[
        WorkflowStep(id="check-orders", provider="claude_code", agent="dev", prompt="go"),
        WorkflowStep(id="deadbeef", provider="claude_code", agent="dev", prompt="go"),
    ],
)
_RUN = "run-view-1"
_TERMINAL = "0badcafe"
# A credential shape ``secret_gate`` redacts (GitHub classic PAT: ``ghp_`` + 36 chars).
_TOKEN = "ghp_" + "Q1w2E3r4T5" * 3 + "Y6u7I8"
# AWS's documented example key id: the one AWS key the repo's .gitleaks.toml allowlists.
_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
_ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


@pytest.fixture(autouse=True)
def _isolated_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "cli_agent_orchestrator.constants.DATABASE_FILE", tmp_path / "wf.db", raising=True
    )
    workflow_journal._event_migrated_paths.clear()
    monkeypatch.setattr(workflow_service, "run_registry", {})
    yield
    workflow_journal._event_migrated_paths.clear()


@pytest.fixture
def log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "terminal-logs"
    directory.mkdir()
    monkeypatch.setattr(workflow_step_view, "TERMINAL_LOG_DIR", directory)
    return directory


@pytest.fixture(autouse=True)
def _clear_scope_overrides():
    yield
    app.dependency_overrides.pop(auth.get_current_scopes, None)


def _seed(*, terminal_id: Any = _TERMINAL, step_id: str = "check-orders", tier: str = "yaml"):
    workflow_journal.insert_run(
        run_id=_RUN,
        workflow_name="nightly-check",
        spec_snapshot=_SPEC.model_dump_json(),
        inputs_json="{}",
        state="running",
        started_at="2026-11-10T00:00:00Z",
        tier=tier,
    )
    workflow_journal.insert_steps(
        _RUN, [("check-orders", "running"), ("deadbeef", "pending")], "2026-11-10T00:00:00Z"
    )
    if terminal_id is not None:
        workflow_journal.set_step_terminal(_RUN, step_id, terminal_id)


class _ReadOnlyBackend:
    """A backend with ONLY ``get_history``: any attach, send or resize would AttributeError."""

    def __init__(self, text: str = "", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls: List[Dict[str, Any]] = []

    def get_history(self, session_name: str, window_name: str, **kwargs: Any) -> str:
        self.calls.append({"session": session_name, "window": window_name, **kwargs})
        if self.error is not None:
            raise self.error
        return self.text


class _WrappingBackend(_ReadOnlyBackend):
    """Returns ``rows`` as tmux renders them at the pane width, or ``joined`` with -J."""

    def __init__(self, rows: str, joined: str) -> None:
        super().__init__(rows)
        self.joined = joined

    def get_history(self, session_name: str, window_name: str, **kwargs: Any) -> str:
        rows = super().get_history(session_name, window_name, **kwargs)
        return self.joined if kwargs.get("join_wrapped") else rows


def _live_terminal(monkeypatch, backend: _ReadOnlyBackend, metadata: Any = "default") -> List[str]:
    looked_up: List[str] = []

    def _metadata(terminal_id: str):
        looked_up.append(terminal_id)
        if metadata == "default":
            return {"tmux_session": "cao-nightly", "tmux_window": "check-orders-1"}
        return metadata

    monkeypatch.setattr(workflow_step_view, "get_terminal_metadata", _metadata)
    monkeypatch.setattr(workflow_step_view, "get_backend", lambda: backend)
    return looked_up


def _screen(client, step_id: str = "check-orders", run_id: str = _RUN):
    return client.get(f"/workflows/runs/{run_id}/steps/{step_id}/screen")


def _transcript(client, step_id: str = "check-orders", run_id: str = _RUN):
    return client.get(f"/workflows/runs/{run_id}/steps/{step_id}/transcript")


# ---------------------------------------------------------------------------
# Inspection: steps[].result
# ---------------------------------------------------------------------------
def test_inspection_carries_the_answer_and_the_terminal_id(client):
    _seed()
    envelope = build_envelope(f"FAIL: rotated {_TOKEN}", "completed", _TERMINAL)
    workflow_journal.set_step_result(_RUN, "check-orders", serialise_envelope(envelope))

    body = client.get(f"/workflows/runs/{_RUN}").json()
    step = {s["id"]: s for s in body["steps"]}["check-orders"]
    assert step["terminal_id"] == _TERMINAL
    assert step["result"] == {
        "text": "FAIL: rotated [REDACTED:github_pat]",
        "status": "completed",
        "truncated": False,
        "redacted": True,
    }


def test_inspection_result_is_null_for_a_row_without_an_envelope(client):
    _seed(terminal_id=None)
    body = client.get(f"/workflows/runs/{_RUN}").json()
    assert [(s["id"], s["result"], s["terminal_id"]) for s in body["steps"]] == [
        ("check-orders", None, None),
        ("deadbeef", None, None),
    ]


@pytest.mark.parametrize("corrupt", ["{not json", '{"unexpected": "shape"}', "[]", '"text"'])
def test_inspection_result_is_null_for_a_corrupt_envelope(client, corrupt):
    _seed()
    workflow_journal.set_step_result(_RUN, "check-orders", corrupt)
    resp = client.get(f"/workflows/runs/{_RUN}")
    assert resp.status_code == 200
    assert {s["id"]: s for s in resp.json()["steps"]}["check-orders"]["result"] is None


def test_a_script_run_gets_result_from_the_envelope_its_settle_writes(client):
    _seed(terminal_id=None, tier="script")
    workflow_journal.settle_step(
        run_id=_RUN,
        step_id="check-orders",
        state="completed",
        updated_at="2026-11-10T00:00:02Z",
        result_json=serialise_envelope(build_envelope("script answer", "completed", None)),
        output_json=None,
        error=None,
    )
    steps = {s["id"]: s for s in client.get(f"/workflows/runs/{_RUN}").json()["steps"]}
    assert steps["check-orders"]["result"]["text"] == "script answer"


# ---------------------------------------------------------------------------
# GET .../screen
# ---------------------------------------------------------------------------
def test_screen_returns_a_read_only_plain_text_capture(client, monkeypatch):
    _seed()
    # The first captured line is always dropped, because it can start mid-line (see
    # test_screen_drops_its_first_captured_line). -J keeps written trailing spaces.
    backend = _ReadOnlyBackend("$ mock_cli\nMockCli ready.\n> MOCK: working  \n\n\n   \n")
    looked_up = _live_terminal(monkeypatch, backend)

    resp = _screen(client)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"terminal_id", "text", "truncated", "redacted", "captured_at"}
    assert body["terminal_id"] == _TERMINAL
    assert body["text"] == "MockCli ready.\n> MOCK: working"
    assert (body["truncated"], body["redacted"]) == (False, False)
    assert _ISO_UTC.match(body["captured_at"])
    assert looked_up == [_TERMINAL]
    assert backend.calls == [
        {
            "session": "cao-nightly",
            "window": "check-orders-1",
            "tail_lines": 250,
            "strip_escapes": True,
            "join_wrapped": True,
        }
    ]


def test_screen_redacts_secrets(client, monkeypatch):
    _seed()
    _live_terminal(monkeypatch, _ReadOnlyBackend(f"$ env\nexporting {_TOKEN}\n❯ "))
    body = _screen(client).json()
    assert _TOKEN not in body["text"]
    assert "[REDACTED:github_pat]" in body["text"]
    assert body["redacted"] is True


def test_screen_rejoins_wrapped_rows_so_a_secret_that_wraps_is_redacted(client, monkeypatch):
    """tmux splits a long line into rows at the pane width, and a pattern matches a secret
    only whole. Read as rows, a key that crossed a wrap came back as two unredacted halves
    with ``redacted: false`` (#1309 review). The screen asks for the rows rejoined (-J)."""
    _seed()
    line = "x" * 50 + _AWS_KEY  # on a 60-column pane the wrap falls 10 characters into the key
    backend = _WrappingBackend(
        rows=f"$ env\n{line[:60]}\n{line[60:]}\n❯ ",
        joined=f"$ env\n{line}\n❯ ",
    )
    _live_terminal(monkeypatch, backend)
    body = _screen(client).json()
    assert backend.calls[0]["join_wrapped"] is True
    for fragment in (_AWS_KEY[:10], _AWS_KEY[10:]):
        assert fragment not in body["text"]
    assert "[REDACTED:aws_access_key]" in body["text"]
    assert body["redacted"] is True


def test_screen_drops_its_first_captured_line(client, monkeypatch):
    """The capture starts 250 rows up, and joined rows begin there even mid-line, so its
    first line can be the second half of a key that began above the window, which no
    pattern matches. Dropping 50 of 250 ROWS used to cover that edge, but 250 joined rows
    can be fewer than 200 lines. So the first line is dropped before redaction."""
    _seed()
    tail = _AWS_KEY[10:]
    _live_terminal(monkeypatch, _ReadOnlyBackend(f"{tail} was exported\n$ next\n❯ "))
    body = _screen(client).json()
    assert tail not in body["text"]
    assert body["text"] == "$ next\n❯"


def test_screen_keeps_the_last_200_lines(client, monkeypatch):
    _seed()
    lines = [f"line {i}" for i in range(300)]
    _live_terminal(monkeypatch, _ReadOnlyBackend("\n".join(lines)))
    body = _screen(client).json()
    assert body["text"].split("\n") == lines[-WORKFLOW_STEP_SCREEN_MAX_LINES:]
    assert body["truncated"] is True


def test_screen_is_capped_at_64_kib_from_a_line_start(client, monkeypatch):
    _seed()
    lines = [f"{i:03d}" + "y" * 500 for i in range(200)]
    _live_terminal(monkeypatch, _ReadOnlyBackend("\n".join(lines)))
    body = _screen(client).json()
    assert len(body["text"].encode("utf-8")) <= WORKFLOW_STEP_SCREEN_MAX_BYTES
    returned = body["text"].split("\n")
    assert returned[-1] == lines[-1]
    assert returned[0] in lines  # starts at a whole line, never mid-line
    assert body["truncated"] is True


def test_screen_redacts_before_cutting_at_the_byte_edge(client, monkeypatch):
    """A secret straddles the 64 KiB cut inside a line with no newline to align to.

    Line alignment cannot help here, so only redacting BEFORE the cut keeps the token's
    second half out: cut first, and the kept text would start with a fragment no pattern
    matches.
    """
    _seed()
    # The raw cut lands 20 bytes into the 40-byte token. The padding is not alphanumeric,
    # so the greedy token pattern cannot swallow it and shrink the text below the cap.
    line = "." * 100 + " " + _TOKEN + " " + "." * (WORKFLOW_STEP_SCREEN_MAX_BYTES - 21)
    _live_terminal(monkeypatch, _ReadOnlyBackend("$ cat config\n" + line))
    body = _screen(client).json()
    assert len(body["text"].encode("utf-8")) <= WORKFLOW_STEP_SCREEN_MAX_BYTES
    for fragment in (_TOKEN[20:], _TOKEN[-12:], _TOKEN[:12]):
        assert fragment not in body["text"]
    assert body["redacted"] is True and body["truncated"] is True


@pytest.mark.parametrize(
    "run_id, step_id",
    [("bad.run", "check-orders"), (_RUN, "bad.step"), (_RUN, "s" * 65), ("r" * 65, "x")],
)
def test_screen_rejects_a_malformed_key(client, monkeypatch, run_id, step_id):
    _seed()
    _live_terminal(monkeypatch, _ReadOnlyBackend("never read"))
    assert _screen(client, step_id=step_id, run_id=run_id).status_code == 400


def test_screen_404s_for_an_unknown_run_step_or_missing_terminal(client, monkeypatch):
    backend = _ReadOnlyBackend("never read")
    _live_terminal(monkeypatch, backend)
    assert _screen(client, run_id="no-such-run").status_code == 404
    _seed()
    assert _screen(client, step_id="no-such-step").status_code == 404
    resp = _screen(client, step_id="deadbeef")  # a step with no terminal recorded yet
    assert resp.status_code == 404
    assert "no terminal recorded" in resp.json()["detail"]
    assert backend.calls == []


@pytest.mark.parametrize("hostile", ["../../etc/x", "ABCDEF12", "0badcafe0", "0badcaf"])
def test_screen_refuses_a_recorded_id_that_is_not_a_terminal_id(client, monkeypatch, hostile):
    _seed(terminal_id=hostile)
    looked_up = _live_terminal(monkeypatch, _ReadOnlyBackend("never read"))
    assert _screen(client).status_code == 404
    assert looked_up == []


def test_screen_410s_when_the_terminal_has_ended(client, monkeypatch):
    _seed()
    _live_terminal(monkeypatch, _ReadOnlyBackend("never read"), metadata=None)
    resp = _screen(client)
    assert resp.status_code == 410
    assert "transcript" in resp.json()["detail"]


@pytest.mark.parametrize(
    "error",
    [ValueError("Session 'cao-nightly' not found"), TerminalNotFoundError(_TERMINAL)],
)
def test_screen_410s_when_the_pane_is_already_gone(client, monkeypatch, error):
    _seed()
    _live_terminal(monkeypatch, _ReadOnlyBackend(error=error))
    assert _screen(client).status_code == 410


def test_screen_500s_without_echoing_an_unexpected_failure(client, monkeypatch):
    _seed()
    _live_terminal(monkeypatch, _ReadOnlyBackend(error=RuntimeError("tmux listing unreadable")))
    resp = _screen(client)
    assert resp.status_code == 500
    assert resp.json() == {"detail": "failed to capture the step's screen"}


# ---------------------------------------------------------------------------
# GET .../transcript
# ---------------------------------------------------------------------------
def test_transcript_returns_the_saved_scrollback(client, log_dir):
    _seed()
    (log_dir / f"{_TERMINAL}.scrollback").write_text("> MOCK: echo\n❯ \n", encoding="utf-8")
    resp = _transcript(client)
    assert resp.status_code == 200
    assert resp.json() == {
        "terminal_id": _TERMINAL,
        "text": "> MOCK: echo\n❯ \n",
        "truncated": False,
        "redacted": False,
    }


def test_transcript_redacts_secrets(client, log_dir):
    _seed()
    (log_dir / f"{_TERMINAL}.scrollback").write_text(f"key {_TOKEN}\n", encoding="utf-8")
    body = _transcript(client).json()
    assert _TOKEN not in body["text"]
    assert body["redacted"] is True


def test_transcript_keeps_the_last_256_kib_from_a_line_start(client, log_dir):
    _seed()
    lines = [f"{i:06d} " + "t" * 90 for i in range(4000)]  # ~388 KiB
    (log_dir / f"{_TERMINAL}.scrollback").write_text("\n".join(lines) + "\n", encoding="utf-8")
    body = _transcript(client).json()
    assert len(body["text"].encode("utf-8")) <= WORKFLOW_STEP_TRANSCRIPT_MAX_BYTES
    returned = body["text"].rstrip("\n").split("\n")
    assert returned[-1] == lines[-1]
    assert returned[0] in lines  # a whole line
    assert returned == lines[lines.index(returned[0]) :]
    assert body["truncated"] is True


def test_a_secret_straddling_the_256_kib_edge_is_not_returned_in_part(client, log_dir):
    """The token spans the 256 KiB cut inside one long line with no newline to align to.

    Only redacting BEFORE the cut keeps its second half out of the returned text.
    """
    _seed()
    # MAX + 121 bytes: under the read window, so the whole file is read, and the raw
    # 256 KiB cut lands 20 bytes into the 40-byte token. The padding is not
    # alphanumeric, so the greedy token pattern cannot swallow it.
    body_text = "." * 100 + " " + _TOKEN + " " + "." * (WORKFLOW_STEP_TRANSCRIPT_MAX_BYTES - 21)
    (log_dir / f"{_TERMINAL}.scrollback").write_text(body_text, encoding="utf-8")

    body = _transcript(client).json()
    assert len(body["text"].encode("utf-8")) <= WORKFLOW_STEP_TRANSCRIPT_MAX_BYTES
    for fragment in (_TOKEN[20:], _TOKEN[-12:], _TOKEN[:12]):
        assert fragment not in body["text"]
    assert body["redacted"] is True and body["truncated"] is True


def test_a_secret_cut_by_the_read_window_start_is_not_returned(client, log_dir):
    """The read window starts inside a token; that partial first line is never returned.

    The tail is mostly long JWT-shaped lines, which redaction shrinks to short markers,
    so the redacted window fits in 256 KiB and nothing else trims its first line. Only
    dropping the partial first line before redaction keeps the token's second half out.
    """
    _seed()
    window = WORKFLOW_STEP_TRANSCRIPT_MAX_BYTES + WORKFLOW_STEP_TRANSCRIPT_MARGIN_BYTES
    jwt_line = "eyJ" + "a" * 60 + ".eyJ" + "b" * 60 + "." + "c" * 60 + "\n"
    tail = jwt_line * (window // len(jwt_line))
    over = window - len(tail.encode("utf-8"))  # bytes of the long line inside the window
    # The window starts 20 bytes into the 40-byte token.
    long_line = "p" * 1000 + _TOKEN + "p" * (over - 21) + "\n"
    (log_dir / f"{_TERMINAL}.scrollback").write_text(long_line + tail, encoding="utf-8")

    body = _transcript(client).json()
    assert len(body["text"].encode("utf-8")) < WORKFLOW_STEP_TRANSCRIPT_MAX_BYTES // 4
    assert body["text"].startswith("[REDACTED:jwt]\n")
    for fragment in (_TOKEN[20:], _TOKEN[-12:]):
        assert fragment not in body["text"]
    assert body["truncated"] is True


def test_transcript_404s_without_a_file(client, log_dir):
    _seed()
    resp = _transcript(client)
    assert resp.status_code == 404
    assert "no transcript was saved" in resp.json()["detail"]


def test_transcript_refuses_a_symlink_or_a_directory(client, log_dir, tmp_path):
    _seed()
    target = tmp_path / "elsewhere.txt"
    target.write_text("not a transcript", encoding="utf-8")
    link = log_dir / f"{_TERMINAL}.scrollback"
    os.symlink(target, link)
    assert _transcript(client).status_code == 404
    link.unlink()
    link.mkdir()
    assert _transcript(client).status_code == 404


def test_transcript_404s_and_400s_like_the_screen(client, log_dir):
    assert _transcript(client, run_id="no-such-run").status_code == 404
    _seed()
    assert _transcript(client, step_id="no-such-step").status_code == 404
    assert _transcript(client, step_id="deadbeef").status_code == 404
    assert _transcript(client, step_id="bad.step").status_code == 400


def test_transcript_reads_the_journals_terminal_never_one_named_by_the_request(
    client, log_dir, monkeypatch
):
    """The request names a step; the file is the one its JOURNAL ROW points at."""
    _seed(step_id="deadbeef")  # the step id is itself shaped like a terminal id
    (log_dir / "deadbeef.scrollback").write_text("decoy: named after the request\n")
    (log_dir / f"{_TERMINAL}.scrollback").write_text("the recorded terminal's transcript\n")
    opened: List[str] = []
    real_open = os.open

    def _spy_open(path, flags, *args):
        opened.append(str(path))
        return real_open(path, flags, *args)

    monkeypatch.setattr(workflow_step_view.os, "open", _spy_open)
    body = _transcript(client, step_id="deadbeef").json()
    assert body["text"] == "the recorded terminal's transcript\n"
    assert opened == [str(log_dir / f"{_TERMINAL}.scrollback")]


@pytest.mark.parametrize("hostile", ["../../etc/passwd", "../" + _TERMINAL, "ABCDEF12", ""])
def test_transcript_never_opens_a_path_for_a_hostile_recorded_id(
    client, log_dir, monkeypatch, hostile
):
    _seed(terminal_id=hostile)
    opened: List[str] = []
    monkeypatch.setattr(workflow_step_view.os, "open", lambda path, *a: opened.append(path))
    assert _transcript(client).status_code == 404
    assert opened == []


# ---------------------------------------------------------------------------
# Scope gate and route resolution
# ---------------------------------------------------------------------------
def _scopes(scopes):
    async def _dep():
        return list(scopes)

    return _dep


@pytest.mark.parametrize("suffix", ["screen", "transcript"])
def test_both_routes_are_scope_gated(client, monkeypatch, suffix):
    monkeypatch.setenv("CAO_AUTH_JWKS_URI", "https://idp.example/jwks")
    url = f"/workflows/runs/{_RUN}/steps/check-orders/{suffix}"
    app.dependency_overrides[auth.get_current_scopes] = _scopes([])
    assert client.get(url).status_code == 403
    for scope in (auth.SCOPE_READ, auth.SCOPE_WRITE, auth.SCOPE_ADMIN):
        app.dependency_overrides[auth.get_current_scopes] = _scopes([scope])
        assert client.get(url).status_code == 404  # admitted; the run is unknown


@pytest.mark.parametrize(
    "path, name",
    [
        ("/workflows/runs/r1/steps/s1/screen", "get_workflow_step_screen_endpoint"),
        ("/workflows/runs/r1/steps/s1/transcript", "get_workflow_step_transcript_endpoint"),
    ],
)
def test_both_routes_resolve_to_their_own_handlers(path, name):
    scope = {"type": "http", "method": "GET", "path": path, "headers": []}
    for route in app.router.routes:
        try:
            match, _ = route.matches(scope)
        except Exception:
            continue
        if match == Match.FULL:
            assert route.name == name
            return
    pytest.fail(f"no route matched {path}")


def test_the_envelope_fixture_is_the_shape_the_engine_stores():
    """Guard for the tests above: they seed exactly what ``build_envelope`` produces."""
    stored = json.loads(serialise_envelope(build_envelope("x", "completed", _TERMINAL)))
    assert set(stored) == {"last_message", "status", "terminal_id", "truncated", "redacted"}
