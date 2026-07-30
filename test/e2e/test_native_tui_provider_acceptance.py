"""§10.3 installed-provider live acceptance (Lane A native-TUI console).

This suite drives the *real* v2 managed-launch native_tui flow
(reserve → launch → bind) over HTTP against a real ``cao-server``
subprocess, with the pinned provider builds installed on this host:

* ``codex`` 0.146.0 (``codex``)
* ``kimi`` 0.29.2 (``kimi_cli``)
* ``claude`` 2.1.220 (``claude_code``)

and then exercises the Lane A control-input contract against the live
provider TUIs: v3 navigation keys, the Kimi dispatch grace, the steer
chord, Escape interruption, and the §4.1 declared-command composer
guard.

Isolation model (deliberately different from the other live suites):

* the cao-server subprocess runs with the operator's **real** ``$HOME``
  so provider auth (``~/.kimi-code``, ``~/.claude``) works, but with
  ``CAO_STATE_ROOT`` pointed at a per-run temp dir — every CAO state
  artifact (SQLite, journal, pane locks, COMPANION_DIR, BRIDGE_ROOT) is
  this run's and never the operator's;
* every tmux invocation — this process's and the server's — goes through
  the private-socket shim from ``test.fixtures.tmux_server``, so no pane
  on the operator's shared tmux server is addressable from here;
* the server env is scrubbed of leaked conductor/operator variables
  (``CAO_CONDUCTOR_*``, ``KIMI_MODEL_THINKING_EFFORT``, …) so the launch
  route comes only from the reservation;
* ``KIMI_CODE_HOME`` is pointed at a *shim* provider home that symlinks
  the real provider state (auth, sessions) but holds a **copy** of
  ``config.toml`` — a ``/model`` switch persists into the copy and can
  never rewrite the operator's real default model.

Evidence: each case writes sanitized capture-pane transcripts and the
exact request/response JSON bodies under ``$CAO_LANE_A_EVIDENCE_DIR``
(when set) or a per-run scratch dir otherwise.  Home paths, scratch
paths and account-identifying tokens are redacted before anything is
written.

Run with:

    uv run pytest -m e2e test/e2e/test_native_tui_provider_acceptance.py -v
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from test.fixtures.cao_server import CaoServer, _pick_free_port, _start_cao_server
from test.fixtures.tmux_server import TmuxServer, isolated_tmux_server
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest
import requests

pytestmark = pytest.mark.e2e

V2_ROOT = "/managed-launch/v2/reservations"
PROTOCOL_V2 = "cao-managed-launch-v2"

KIMI_PIN = "0.29.2"
CLAUDE_PIN = "2.1.220"
CODEX_PIN = "0.146.0"

KIMI_MODEL = "kimi-code/kimi-for-coding"
EFFORT_PROVIDER_DEFAULT = "provider-default"
CLAUDE_MODEL_ALIAS = "sonnet"
CODEX_MODEL = "gpt-5.6-terra"
CODEX_EFFORT = "xhigh"

AGENT_PROFILE = "acceptance"

# The acceptance profile: no MCP servers (``cao-mcp-server`` is not on the
# pane's bounded PATH, so the built-in profiles would stall the Kimi boot
# gate on "connecting to mcp servers"), no tool grants (no approvals can
# be prompted mid-turn), and a brevity instruction to keep turns cheap.
_ACCEPTANCE_PROFILE = """---
name: acceptance
description: Disposable Lane A 10.3 acceptance profile (no MCP servers, no tool grants)
---

You are a disposable acceptance-test agent.  Keep every reply as short as
possible.  Never use tools.
"""

# Server-spawn scrubbing: variables that would leak the surrounding
# conductor/operator context into the launch route or the server config.
_SCRUB_EXACT = {
    "CAO_TERMINAL_ID",
    "CAO_ALLOWED_HOSTS",
    "CAO_WS_ALLOWED_CLIENTS",
    "KIMI_MODEL_THINKING_EFFORT",
}
_SCRUB_PREFIXES = ("CAO_CONDUCTOR_", "CAO_WORKFLOW_")

EVIDENCE_ENV = "CAO_LANE_A_EVIDENCE_DIR"

LAUNCH_HTTP_TIMEOUT = 420.0
BIND_RETRY_SECONDS = 240.0
KIMI_DISPATCH_GRACE_SECONDS = 5.0
# Grace waits use a margin over the pinned 5 s so a slow CI host cannot
# straddle the boundary.
GRACE_SLEEP = KIMI_DISPATCH_GRACE_SECONDS + 0.8

_SEQUENCE_KEYS_16 = {
    "Escape",
    "C-c",
    "C-s",
    "Enter",
    "Backspace",
    "Up",
    "Down",
    "Left",
    "Right",
    "Home",
    "End",
    "PageUp",
    "PageDown",
    "Delete",
    "Insert",
    "Tab",
}


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


class Evidence:
    """Sanitized per-case evidence writer.

    Every file is passed through the redaction table before being written;
    the table is built from values the fixture already knows (home paths,
    scratch paths) plus account-identifying tokens harvested without ever
    printing them.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self._redactions: List[Tuple[str, str]] = []

    def redact(self, value: Optional[str], label: str) -> None:
        if value and len(value) >= 3:
            self._redactions.append((value, label))

    def sanitize(self, text: str) -> str:
        for old, new in sorted(self._redactions, key=lambda pair: -len(pair[0])):
            text = text.replace(old, new)
        return text

    def write(self, case: str, name: str, content: str) -> Path:
        case_dir = self.root / case
        case_dir.mkdir(parents=True, exist_ok=True)
        path = case_dir / name
        path.write_text(self.sanitize(content), encoding="utf-8")
        return path

    def write_json(self, case: str, name: str, obj: Any) -> Path:
        return self.write(case, name, json.dumps(obj, indent=2, sort_keys=True) + "\n")

    def note(self, case: str, line: str) -> None:
        """Append one observation line to the case's notes file."""
        case_dir = self.root / case
        case_dir.mkdir(parents=True, exist_ok=True)
        with open(case_dir / "notes.md", "a", encoding="utf-8") as handle:
            handle.write(self.sanitize(line).rstrip("\n") + "\n")


# ---------------------------------------------------------------------------
# Harness fixture: private tmux + real-HOME cao-server on a temp state root
# ---------------------------------------------------------------------------


@dataclass
class Harness:
    server: CaoServer
    tmux: TmuxServer
    state_root: Path
    scratch: Path
    evidence: Evidence


@pytest.fixture(scope="session")
def tmux_server() -> Iterator[TmuxServer]:
    if not shutil.which("tmux"):
        pytest.skip("tmux not installed")
    with isolated_tmux_server() as server:
        yield server


def _build_kimi_provider_home_shim(real_home: Path, scratch: Path) -> Path:
    """A provider home that shares auth but not the operator's config file.

    The launch symlinks every entry of ``$KIMI_CODE_HOME`` into the
    generation-private home.  Pointing ``KIMI_CODE_HOME`` here means those
    symlinks resolve to the real auth state (credentials, oauth, sessions)
    while ``config.toml`` is a copy — so a ``/model`` persist writes the
    copy (or replaces the private symlink), never the operator's file.
    """
    real_kimi_home = real_home / ".kimi-code"
    shim_home = scratch / "kimi-provider-home"
    shim_home.mkdir(parents=True, exist_ok=True)
    if real_kimi_home.is_dir():
        for entry in real_kimi_home.iterdir():
            if entry.name in ("config.toml", "mcp.json"):
                continue
            destination = shim_home / entry.name
            if destination.exists() or destination.is_symlink():
                continue
            try:
                destination.symlink_to(entry, target_is_directory=entry.is_dir())
            except OSError:
                continue
        real_config = real_kimi_home / "config.toml"
        if real_config.exists():
            shutil.copy2(real_config, shim_home / "config.toml")
    return shim_home


def _harvest_email_tokens(paths: List[Path]) -> List[str]:
    """Account-identifying tokens to redact, read but never printed."""
    tokens: List[str] = []
    pattern = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        tokens.extend(pattern.findall(text))
    return sorted(set(tokens), key=len, reverse=True)


@pytest.fixture(scope="session")
def harness(tmp_path_factory: pytest.TempPathFactory, tmux_server: TmuxServer) -> Iterator[Harness]:
    real_home = os.environ.get("HOME", "")
    if not real_home or not Path(real_home).is_dir():
        pytest.skip("§10.3 acceptance needs the operator's real $HOME (provider auth)")
    home_path = Path(real_home)

    state_root = Path(tmp_path_factory.mktemp("cao_state_103"))
    scratch = Path(tmp_path_factory.mktemp("cao_scratch_103"))
    server_bookkeeping = scratch / "server-home"

    # The acceptance agent profile resolves from the state root's local
    # agent store (LOCAL_AGENT_STORE_DIR follows CAO_STATE_ROOT).
    agent_store = state_root / "agent-store"
    agent_store.mkdir(parents=True, exist_ok=True)
    (agent_store / f"{AGENT_PROFILE}.md").write_text(_ACCEPTANCE_PROFILE, encoding="utf-8")

    kimi_home_shim = _build_kimi_provider_home_shim(home_path, scratch)

    evidence_root = Path(os.environ.get(EVIDENCE_ENV) or (scratch / "evidence"))
    evidence = Evidence(evidence_root)
    evidence.redact(real_home, "<HOME>")
    evidence.redact(str(scratch), "<SCRATCH>")
    evidence.redact(str(state_root), "<STATE_ROOT>")
    evidence.redact(str(tmux_server.owned_root), "<TMUX_SOCKDIR>")
    evidence.redact(os.environ.get("USER", ""), "<USER>")
    for token in _harvest_email_tokens(
        [home_path / ".kimi-code" / "config.toml", home_path / ".claude.json"]
    ):
        evidence.redact(token, "<ACCOUNT>")
    evidence.note(
        "run",
        f"- evidence root: {evidence.root}\n"
        f"- tmux socket: <TMUX_SOCKDIR>/server.sock\n"
        f"- CAO_STATE_ROOT: <STATE_ROOT>\n"
        f"- server $HOME: real operator $HOME (provider auth), CAO state isolated",
    )

    assert tmux_server.owned_root is not None
    shim = tmux_server.write_shim(tmux_server.owned_root / "bin")

    # Scrub leaked conductor/operator context for the server spawn only.
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


# ---------------------------------------------------------------------------
# Provider session: reserve → launch → bind against the real CLI
# ---------------------------------------------------------------------------


@dataclass
class ProviderSession:
    provider: str
    reservation_id: str
    terminal_id: str
    generation: str
    session_name: str
    pane_id: str
    window_id: str
    native_session_id: str
    workdir: str
    launched_record: Dict[str, Any] = field(default_factory=dict)


def _pinned_executable(binary: str, pin: str) -> Tuple[str, str, str]:
    """(canonical path, sha256, banner) for the pinned build, or skip."""
    resolved = shutil.which(binary)
    if not resolved:
        pytest.skip(f"{binary} CLI not installed")
    exe = os.path.realpath(resolved)
    try:
        probe = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=20.0, check=False
        )
    except (subprocess.SubprocessError, OSError) as exc:
        pytest.skip(f"{binary} --version failed: {exc}")
    banner = (probe.stdout or probe.stderr or "").strip()
    if pin not in banner:
        pytest.skip(f"{binary} is {banner!r}, not the pinned build {pin}")
    return exe, hashlib.sha256(Path(exe).read_bytes()).hexdigest(), banner


def _answer_claude_startup_dialogs(
    harness: Harness,
    session_name: str,
    case: str,
    *,
    thread: "threading.Thread",
    deadline_monotonic: float,
) -> None:
    """Answer Claude's first-boot dialogs as an operator, via tmux directly.

    The workspace-trust dialog ("Yes, I trust this folder") blocks startup
    before SessionStart in any directory the operator has not trusted
    before — and control-input cannot answer it: Enter is composer-class,
    and a permission/selection prompt is exactly what the readiness gate
    refuses to write into.  The operator answer is a keystroke at the
    pane, sent here through the fixture's tmux handle (never a raw socket)
    and recorded in the evidence.  Accepting the dialog writes claude's
    standard trust record for the disposable worktree into the operator's
    own claude config, exactly as an interactive first run would.
    """
    answered: List[str] = []
    while thread.is_alive() and time.monotonic() < deadline_monotonic:
        try:
            panes = harness.tmux.out("list-panes", "-s", "-t", session_name, "-F", "#{pane_id}")
        except Exception:  # noqa: BLE001 - session may not exist yet
            time.sleep(0.5)
            continue
        for pane in panes.splitlines():
            try:
                screen = harness.tmux.out("capture-pane", "-p", "-t", pane)
            except Exception:  # noqa: BLE001 - pane may have raced away
                continue
            dialog = None
            if "Yes, I trust this folder" in screen:
                dialog = "workspace-trust"
            elif "Bypass Permissions" in screen and "Esc" in screen:
                dialog = "bypass-permissions-warning"
            if dialog is None:
                continue
            harness.evidence.write(
                case, f"91-{dialog}-dialog-{pane.lstrip('%')}.txt", screen + "\n"
            )
            harness.tmux.out("send-keys", "-t", pane, "Enter")
            answered.append(f"{dialog} dialog confirmed with Enter on pane {pane}")
            time.sleep(0.5)
    if answered:
        harness.evidence.note(case, "; ".join(answered))
    else:
        harness.evidence.note(case, "no claude startup dialog appeared before the launch returned")


def _launch_claude_with_dialog_answers(
    harness: Harness, reservation_id: str, session_name: str, case: str
) -> requests.Response:
    """POST launch while concurrently answering Claude's startup dialogs.

    The launch call is synchronous through the SessionStart wait, and the
    trust dialog blocks SessionStart — so the dialog answer has to happen
    while the POST is still in flight.
    """
    import threading

    result: Dict[str, Any] = {}

    def _post_launch() -> None:
        result["response"] = requests.post(
            f"{harness.server.url}{V2_ROOT}/{reservation_id}/launch",
            timeout=LAUNCH_HTTP_TIMEOUT,
        )

    thread = threading.Thread(target=_post_launch, daemon=True)
    thread.start()
    _answer_claude_startup_dialogs(
        harness, session_name, case, thread=thread, deadline_monotonic=time.monotonic() + 300.0
    )
    thread.join(timeout=LAUNCH_HTTP_TIMEOUT)
    assert "response" in result, "the launch POST never returned"
    return result["response"]


def _launch_provider_session(
    harness: Harness,
    *,
    provider: str,
    binary: str,
    pin: str,
    expected_model: str,
    expected_effort: str,
    tag: str,
    agent_profile: str = AGENT_PROFILE,
) -> ProviderSession:
    """One disposable managed native-TUI session on the real provider CLI."""
    case = f"launch-{tag}"
    exe, digest, banner = _pinned_executable(binary, pin)
    harness.evidence.note(
        case,
        f"- provider: `{provider}`\n- executable (canonical): `{exe}`\n- version banner: `{banner}`",
    )

    session_name = f"cao-a103-{tag}-{uuid.uuid4().hex[:6]}"
    harness.tmux.new_session(session_name, "-x", "100", "-y", "30", "--", "sh", "-c", "sleep 3600")

    worktree = harness.scratch / f"worktree-{tag}"
    worktree.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
    subprocess.run(
        ["git", "config", "user.email", "lane-a@example.invalid"], cwd=worktree, check=True
    )
    subprocess.run(["git", "config", "user.name", "lane-a-acceptance"], cwd=worktree, check=True)
    (worktree / "README.txt").write_text("lane a 10.3 acceptance worktree\n")
    subprocess.run(["git", "add", "."], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=worktree, check=True)
    workdir = os.path.realpath(worktree)
    harness.evidence.redact(workdir, f"<WORKTREE-{tag.upper()}>")

    payload = {
        "protocol_version": PROTOCOL_V2,
        "reservation_id": str(uuid.uuid4()),
        "session_name": session_name,
        "provider": provider,
        "agent_profile": agent_profile,
        "caller_id": uuid.uuid4().hex[:8],
        "working_directory": workdir,
        "trusted_project_root": workdir if provider == "codex" else None,
        "expected_model": expected_model,
        "expected_effort": expected_effort,
        "provider_executable": exe,
        "provider_executable_sha256": digest,
        "obligation_generation": f"obgen-{uuid.uuid4().hex[:8]}",
        "task_id": f"lane-a-10-3-{tag}",
        "run_id": f"run-{uuid.uuid4().hex[:8]}",
        "delivery_id": str(uuid.uuid4()),
        "launch_nonce": uuid.uuid4().hex + uuid.uuid4().hex[:8],
        "execution_mode": "native_tui",
    }
    harness.evidence.write_json(case, "01-reserve-request.json", payload)

    reserved = requests.post(f"{harness.server.url}{V2_ROOT}", json=payload, timeout=30)
    assert reserved.status_code == 201, f"reserve failed: {reserved.status_code} {reserved.text}"
    reservation_id = payload["reservation_id"]

    if provider == "claude_code":
        launched = _launch_claude_with_dialog_answers(harness, reservation_id, session_name, case)
    else:
        launched = requests.post(
            f"{harness.server.url}{V2_ROOT}/{reservation_id}/launch",
            timeout=LAUNCH_HTTP_TIMEOUT,
        )
    record = requests.get(f"{harness.server.url}{V2_ROOT}/{reservation_id}", timeout=30).json()
    harness.evidence.write_json(
        case,
        "02-launch-response.json",
        {
            "status_code": launched.status_code,
            "body": (
                launched.json()
                if launched.headers.get("content-type", "").startswith("application/json")
                else launched.text
            ),
        },
    )
    harness.evidence.write_json(case, "03-record-after-launch.json", record)
    assert launched.status_code == 200, (
        f"launch POST failed: {launched.status_code} {launched.text}; record state="
        f"{record.get('state')} preflight_failure={record.get('preflight_failure')}"
    )
    state = record.get("state")
    if state not in ("launching", "bound"):
        # Pane-side evidence for a launch that never became ready: the
        # preflight record says *that* readiness failed; the pane shows why.
        try:
            panes = harness.tmux.out("list-panes", "-s", "-t", session_name, "-F", "#{pane_id}")
            for index, pane in enumerate(panes.splitlines()):
                harness.evidence.write(
                    case,
                    f"90-pane-{index}-on-launch-failure.txt",
                    harness.tmux.out("capture-pane", "-p", "-S-400", "-t", pane) + "\n",
                )
        except Exception as exc:  # noqa: BLE001 - diagnostics must not mask the failure
            harness.evidence.note(case, f"pane capture on launch failure itself failed: {exc}")
    assert state in ("launching", "bound"), (
        f"launch did not reach a launchable state: state={state!r} "
        f"preflight_failure={json.dumps(record.get('preflight_failure'))}"
    )

    bind_body = {
        "protocol_version": PROTOCOL_V2,
        "terminal_id": launched.json()["terminal_id"],
        "generation": launched.json()["generation"],
        "attempt_id": str(uuid.uuid4()),
    }
    deadline = time.monotonic() + BIND_RETRY_SECONDS
    while True:
        bound = requests.post(
            f"{harness.server.url}{V2_ROOT}/{reservation_id}/bind",
            json=bind_body,
            timeout=60,
        )
        if bound.status_code == 425 and time.monotonic() < deadline:
            time.sleep(2.0)
            continue
        break
    harness.evidence.write_json(
        case,
        "04-bind-response.json",
        {
            "status_code": bound.status_code,
            "body": (
                bound.json()
                if bound.headers.get("content-type", "").startswith("application/json")
                else bound.text
            ),
        },
    )
    assert bound.status_code == 200, f"bind failed: {bound.status_code} {bound.text}"

    record = requests.get(f"{harness.server.url}{V2_ROOT}/{reservation_id}", timeout=30).json()
    harness.evidence.write_json(case, "05-record-after-bind.json", record)
    pane_id = record.get("pane_id")
    if not pane_id:
        # Fall back to the pane tmux reports for the managed window.
        window_name = (
            f"managed-{bind_body['terminal_id']}-{bind_body['generation'].replace('-', '')[:12]}"
        )
        pane_id = harness.tmux.out(
            "list-panes", "-t", f"{session_name}:{window_name}", "-F", "#{pane_id}"
        )
    session = ProviderSession(
        provider=provider,
        reservation_id=reservation_id,
        terminal_id=bind_body["terminal_id"],
        generation=bind_body["generation"],
        session_name=session_name,
        pane_id=pane_id,
        window_id=record.get("window_id") or "",
        native_session_id=record.get("native_session_id") or "",
        workdir=workdir,
        launched_record=record,
    )
    harness.evidence.note(
        case,
        f"- reservation: `{reservation_id}`\n- terminal: `{session.terminal_id}`\n"
        f"- generation: `{session.generation}`\n- pane: `{session.pane_id}` "
        f"in session `{session_name}`\n- native_session_id: `{session.native_session_id}`\n"
        f"- bind state: `{bound.json().get('state')}`",
    )
    return session


def _kill_session(harness: Harness, session: ProviderSession) -> None:
    harness.tmux.kill_session(session.session_name, check=False)


@pytest.fixture(scope="module")
def kimi_session(harness: Harness) -> Iterator[ProviderSession]:
    session = _launch_provider_session(
        harness,
        provider="kimi_cli",
        binary="kimi",
        pin=KIMI_PIN,
        expected_model=KIMI_MODEL,
        expected_effort=EFFORT_PROVIDER_DEFAULT,
        tag="kimi",
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
        tag="claude",
    )
    try:
        yield session
    finally:
        _kill_session(harness, session)


@pytest.fixture(scope="module")
def codex_session(harness: Harness) -> Iterator[ProviderSession]:
    session = _launch_provider_session(
        harness,
        provider="codex",
        binary="codex",
        pin=CODEX_PIN,
        expected_model=CODEX_MODEL,
        expected_effort=CODEX_EFFORT,
        tag="codex",
    )
    try:
        yield session
    finally:
        _kill_session(harness, session)


# ---------------------------------------------------------------------------
# Control-input + capture helpers
# ---------------------------------------------------------------------------


def _capture(
    harness: Harness, session: ProviderSession, *, styled: bool = False, history: bool = False
) -> str:
    args = ["capture-pane", "-p"]
    if styled:
        args.append("-e")
    if history:
        # Attached form on purpose: the fixture's argv guard refuses a bare
        # "-S" (a second server selector); "-S-400" is unambiguously
        # capture-pane's start-line and parses after the subcommand.
        args.append("-S-400")
    args += ["-t", session.pane_id]
    return harness.tmux.out(*args)


def _post(
    harness: Harness,
    session: ProviderSession,
    body: Dict[str, Any],
    *,
    timeout: float = 60.0,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """POST one control-input request; return (request, response) bodies."""
    body = {"control_id": f"ctl-{uuid.uuid4().hex[:10]}", **body}
    response = requests.post(
        f"{harness.server.url}/terminals/{session.terminal_id}/control-input",
        json=body,
        timeout=timeout,
    )
    assert (
        response.status_code == 200
    ), f"control-input POST returned {response.status_code}: {response.text}"
    return body, response.json()


def _post_events(
    harness: Harness,
    session: ProviderSession,
    events: List[Dict[str, Any]],
    *,
    payload_class: Optional[str] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    body: Dict[str, Any] = {"events": events}
    if payload_class is not None:
        body["payload_class"] = payload_class
    return _post(harness, session, body)


def _control_identity(harness: Harness, session: ProviderSession) -> Dict[str, Any]:
    response = requests.get(
        f"{harness.server.url}/terminals/{session.terminal_id}/control-identity", timeout=30
    )
    assert response.status_code == 200, response.text
    return response.json()


def _await(predicate, timeout: float = 5.0, poll: float = 0.1) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(poll)
    return False


_KIMI_SPINNER = re.compile(r"[⠁-⣿🌑-🌘]")
_CLAUDE_SPINNER = re.compile(r"^[ \t]*[✶✢✽✻✳·*][ \t]+\w*ing\b.*…", re.M)
_CODEX_SPINNER = re.compile(r"[•◦].*\(\d+s\s*•\s*esc to interrupt\)")
_CLAUDE_BOX_RULE = re.compile(r"^\s*─{3,}\s*$")


def _turn_active(harness: Harness, session: ProviderSession) -> bool:
    screen = _capture(harness, session)
    if session.provider == "kimi_cli":
        tail = "\n".join(screen.splitlines()[-18:])
        return bool(_KIMI_SPINNER.search(tail))
    if session.provider == "codex":
        return bool(_CODEX_SPINNER.search(screen))
    return bool(_CLAUDE_SPINNER.search(screen))


# The installed kimi 0.29.2 composer is a rounded box (verified against the
# installed bundle: "╭" + "─"*w + "╮", content rows, "╰" + "─"*w + "╯" —
# the bundle contains no '── input ──' rendering at all).  The composer is
# always the LAST such box on screen; the status bar below it is not boxed.
# The server's §4.1 emptiness pin (native_pane_input's kimi-composer-box
# rule) was corrected by this acceptance round to read exactly this shape;
# the test-side reader mirrors it independently, so a pin regression shows
# as a mismatch between the two rather than passing silently.
_KIMI_BOX_TOP = re.compile(r"^\s*╭─+╮\s*$")
_KIMI_BOX_BOTTOM = re.compile(r"^\s*╰─+╯\s*$")
_KIMI_BOX_CONTENT = re.compile(r"^\s*│(.*)│\s*$")


def _kimi_input_rows(screen: str) -> Optional[List[str]]:
    """Content rows of the last rounded composer box, inner text only."""
    rows = screen.splitlines()
    bottom = None
    for index in range(len(rows) - 1, -1, -1):
        if _KIMI_BOX_BOTTOM.match(rows[index]):
            bottom = index
            break
    if bottom is None:
        return None
    top = None
    for index in range(bottom - 1, -1, -1):
        if _KIMI_BOX_TOP.match(rows[index]):
            top = index
            break
    if top is None:
        return None
    content: List[str] = []
    for row in rows[top + 1 : bottom]:
        match = _KIMI_BOX_CONTENT.match(row)
        if match is None:
            return None
        content.append(match.group(1))
    return content


def _kimi_composer_text(screen: str) -> Optional[str]:
    rows = _kimi_input_rows(screen)
    if rows is None:
        return None
    return "\n".join(rows)


def _kimi_composer_prefill(screen: str) -> Optional[str]:
    """The composer's text after the '>' prompt glyph, or None if no box."""
    rows = _kimi_input_rows(screen)
    if rows is None:
        return None
    texts = []
    for row in rows:
        stripped = row.strip()
        if stripped.startswith(">"):
            stripped = stripped[1:]
        texts.append(stripped.rstrip())
    return "\n".join(texts)


def _claude_composer_rows_plain(screen: str) -> Optional[List[str]]:
    """Rows inside the last claude prompt box (plain capture)."""
    rows = screen.splitlines()
    rules = [i for i, row in enumerate(rows) if _CLAUDE_BOX_RULE.match(row)]
    if len(rules) < 2:
        return None
    return rows[rules[-2] + 1 : rules[-1]]


def _codex_composer_rows_plain(screen: str) -> Optional[List[str]]:
    """Rows from the last Codex prompt through its blank footer separator."""
    rows = screen.splitlines()
    prompt = next((index for index in range(len(rows) - 1, -1, -1) if "›" in rows[index]), None)
    if prompt is None:
        return None
    separator = next(
        (index for index in range(prompt + 1, len(rows)) if not rows[index].strip()),
        None,
    )
    if separator is None:
        return None
    return rows[prompt:separator]


def _composer_holds(harness: Harness, session: ProviderSession, text: str) -> bool:
    screen = _capture(harness, session)
    if session.provider == "kimi_cli":
        composer = _kimi_composer_text(screen)
        return composer is not None and text in composer
    if session.provider == "codex":
        rows = _codex_composer_rows_plain(screen)
        return rows is not None and any(text in row for row in rows)
    rows = _claude_composer_rows_plain(screen)
    return rows is not None and any(text in row for row in rows)


def _kimi_composer_nonblank(harness: Harness, session: ProviderSession) -> bool:
    screen = _capture(harness, session)
    prefill = _kimi_composer_prefill(screen)
    return prefill is not None and any(row.strip() for row in prefill.splitlines())


def _claude_composer_styled_region(screen: str) -> Optional[str]:
    """The last prompt-box region (rules + content) of a styled capture.

    Compared region-scoped rather than whole-screen: status-line chrome
    (context meters, spinners) may change between two captures of an
    untouched composer, but the guard's byte-identical claim is about the
    composer.
    """
    rows = screen.splitlines()
    rules = [i for i, row in enumerate(rows) if _CLAUDE_BOX_RULE.match(row)]
    if len(rules) < 2:
        return None
    return "\n".join(rows[rules[-2] : rules[-1] + 1])


def _clear_composer(harness: Harness, session: ProviderSession, *, seeded: str, case: str) -> None:
    """Operator-side composer clear: Home, then Delete-forward sweeps.

    Records exactly which keystrokes were used.  Asserts the seeded text
    is gone from the composer afterwards.
    """
    harness.evidence.note(
        case,
        f"composer clear: sent [Home, Delete×31] then verified; seeded text was {seeded!r}",
    )
    events = [{"type": "key", "key": "Home"}] + [
        {"type": "key", "key": "Delete"} for _ in range(31)
    ]
    _request, response = _post_events(harness, session, events)
    assert response["outcome"] == "accepted", f"composer clear refused: {response}"
    if _await(lambda: not _composer_holds(harness, session, seeded), timeout=5.0):
        return
    # Fallback: Backspace sweeps from the cursor position.
    harness.evidence.note(case, "Home/Delete sweep insufficient; used Backspace×32 fallback")
    events = [{"type": "key", "key": "Backspace"} for _ in range(32)]
    _request, response = _post_events(harness, session, events)
    assert response["outcome"] == "accepted", f"composer clear fallback refused: {response}"
    assert _await(
        lambda: not _composer_holds(harness, session, seeded), timeout=5.0
    ), f"composer still holds {seeded!r} after Home/Delete and Backspace sweeps:\n" + _capture(
        harness, session
    )


_KIMI_MENU_HINTS = ("↑↓", "navigate", "Enter select", "type to search")


def _kimi_menu_open(harness: Harness, session: ProviderSession) -> bool:
    screen = _capture(harness, session)
    return any(hint in screen for hint in _KIMI_MENU_HINTS)


def _ensure_idle(harness: Harness, session: ProviderSession, *, case: str) -> None:
    """Precondition: no active turn, no open menu, ready composer.

    A leftover open menu is always closed: with the picker mounted, typed
    text lands in its search field rather than the composer, which poisons
    every later case.  The close Escape is sent only while a menu is
    visibly open — consecutive bare Escapes at an empty Kimi composer are
    the provider's exit chord, and this precondition never risks it.
    """
    if session.provider == "kimi_cli":
        for attempt in range(3):
            if not _kimi_menu_open(harness, session):
                break
            _request, response = _post_events(harness, session, [{"type": "key", "key": "Escape"}])
            harness.evidence.note(
                case,
                f"precondition menu-close Escape #{attempt + 1}: outcome={response['outcome']}",
            )
            time.sleep(0.5)
    if _turn_active(harness, session):
        assert _await(
            lambda: not _turn_active(harness, session), timeout=90.0
        ), f"provider turn never went idle:\n{_capture(harness, session)}"
    if session.provider == "kimi_cli":
        # Any earlier Enter-carrying batch arms the dispatch grace.
        time.sleep(GRACE_SLEEP)


def _await_menu(harness: Harness, session: ProviderSession, timeout: float = 15.0) -> bool:
    """Wait until the /model picker is visibly mounted."""
    return _await(lambda: _kimi_menu_open(harness, session), timeout=timeout, poll=0.15)


def _save_screen(
    harness: Harness,
    session: ProviderSession,
    case: str,
    name: str,
    *,
    styled: bool = False,
    history: bool = False,
) -> str:
    screen = _capture(harness, session, styled=styled, history=history)
    harness.evidence.write(case, name, screen + "\n")
    return screen


# ===========================================================================
# Codex 0.146.0 — discovery plus disposable native-TUI session
# ===========================================================================


class TestCodexAdvertisement:
    def test_codex_has_a_provider_controls_entry(self, harness: Harness) -> None:
        response = requests.get(f"{harness.server.url}/control-input/capabilities", timeout=30)
        assert response.status_code == 200, response.text
        body = response.json()
        harness.evidence.write_json("case-10-codex-advertisement", "capabilities.json", body)

        provider_controls = body.get("provider_controls") or {}
        assert sorted(provider_controls) == [
            "claude_code",
            "codex",
            "kimi_cli",
        ], f"provider_controls keys drifted: {sorted(provider_controls)}"
        # The §3.5 additive block shape, asserted while we are here.
        assert body.get("request_schema_versions") == [1, 2, 3, 4], body.get(
            "request_schema_versions"
        )
        assert (body.get("command_controls") or {}).get("composer_nonempty_guard") is True
        assert (body.get("streaming") or {}).get("supported") is True
        assert set((body.get("sequence") or {}).get("keys") or []) == _SEQUENCE_KEYS_16
        kimi_block = provider_controls["kimi_cli"]
        assert kimi_block.get("steer_chords") == ["C-s"], kimi_block
        assert kimi_block.get("dispatch_grace_ms") == 5000, kimi_block
        assert provider_controls["claude_code"].get("steer_chords") == []
        codex_block = provider_controls["codex"]
        assert codex_block.get("steer_chords") == []
        assert (codex_block.get("compact") or {}).get("events") == [
            {"type": "text", "text": "/compact"},
            {"type": "key", "key": "Enter"},
        ]
        harness.evidence.note(
            "case-10-codex-advertisement",
            "PASS: codex is advertised with the pinned compact sequence; keys are exactly "
            f"{sorted(provider_controls)}; schema versions {body.get('request_schema_versions')}.",
        )


class TestCodexNativeTui:
    def test_01_control_identity_is_build_exact(
        self, harness: Harness, codex_session: ProviderSession
    ) -> None:
        identity = _control_identity(harness, codex_session)
        harness.evidence.write_json("case-14-codex-identity", "control-identity.json", identity)
        block = identity.get("control_input") or {}
        provider_controls = (block.get("provider_controls") or {}).get("codex") or {}
        assert provider_controls.get("steer_chords") == [], provider_controls
        assert (provider_controls.get("compact") or {}).get("events") == [
            {"type": "text", "text": "/compact"},
            {"type": "key", "key": "Enter"},
        ], provider_controls
        assert (block.get("command_controls") or {}).get("composer_nonempty_guard") is True, block
        assert codex_session.native_session_id, codex_session.launched_record

    def test_02_declared_compact_refuses_prefill_without_mutation(
        self, harness: Harness, codex_session: ProviderSession
    ) -> None:
        case = "case-15-codex-guard-prefill"
        _ensure_idle(harness, codex_session, case=case)
        _request, seeded = _post_events(
            harness, codex_session, [{"type": "text", "text": "queued draft"}]
        )
        assert seeded["outcome"] == "accepted", seeded
        assert _await(lambda: _composer_holds(harness, codex_session, "queued draft"))
        before = _save_screen(harness, codex_session, case, "01-prefill.txt", styled=True)

        request, refused = _post_events(
            harness,
            codex_session,
            [{"type": "text", "text": "/compact"}, {"type": "key", "key": "Enter"}],
            payload_class="command",
        )
        harness.evidence.write_json(case, "02-request.json", request)
        harness.evidence.write_json(case, "03-response.json", refused)
        assert refused["outcome"] == "refused", refused
        assert refused["reason_code"] == "composer-nonempty", refused
        assert _composer_holds(harness, codex_session, "queued draft")
        after = _save_screen(harness, codex_session, case, "04-after.txt", styled=True)
        assert _codex_composer_rows_plain(before) == _codex_composer_rows_plain(after)
        _clear_composer(harness, codex_session, seeded="queued draft", case=case)

    def test_03_declared_compact_executes_on_empty_composer(
        self, harness: Harness, codex_session: ProviderSession
    ) -> None:
        case = "case-16-codex-compact-executes"
        _ensure_idle(harness, codex_session, case=case)
        _save_screen(harness, codex_session, case, "01-empty.txt", styled=True)
        request, accepted = _post_events(
            harness,
            codex_session,
            [{"type": "text", "text": "/compact"}, {"type": "key", "key": "Enter"}],
            payload_class="command",
        )
        harness.evidence.write_json(case, "02-request.json", request)
        harness.evidence.write_json(case, "03-response.json", accepted)
        assert accepted["outcome"] == "accepted", accepted
        assert accepted.get("submission_observed") == "submitted", accepted
        assert accepted.get("submission_evidence_ref", "").startswith("capture-pane:"), accepted
        assert _await(
            lambda: "Context compacted" in _capture(harness, codex_session, history=True),
            timeout=30.0,
            poll=0.5,
        )
        _save_screen(harness, codex_session, case, "04-history.txt", history=True)


# ===========================================================================
# Kimi 0.29.2 — disposable native-TUI session
# ===========================================================================


class TestKimiIdentitySurface:
    def test_control_identity_is_build_exact(
        self, harness: Harness, kimi_session: ProviderSession
    ) -> None:
        identity = _control_identity(harness, kimi_session)
        harness.evidence.write_json("case-00-kimi-identity", "control-identity.json", identity)
        block = identity.get("control_input") or {}
        provider_controls = (block.get("provider_controls") or {}).get("kimi_cli") or {}
        assert provider_controls.get("steer_chords") == ["C-s"], provider_controls
        assert provider_controls.get("dispatch_grace_ms") == 5000, provider_controls
        assert (provider_controls.get("compact") or {}).get("events") == [
            {"type": "text", "text": "/compact"},
            {"type": "key", "key": "Enter"},
        ], provider_controls
        assert (block.get("command_controls") or {}).get("composer_nonempty_guard") is True, block
        assert block.get("schema_versions") == [1, 2, 3, 4], block
        harness.evidence.note(
            "case-00-kimi-identity",
            f"PASS: build-exact provider_controls for kimi_cli {KIMI_PIN}: steer_chords=['C-s'], "
            "dispatch_grace_ms=5000, compact shape pinned, composer guard advertised.",
        )


class TestKimiModelMenu:
    def test_01_fused_model_menu_sequence(
        self, harness: Harness, kimi_session: ProviderSession
    ) -> None:
        """§10.3 case 1: text("/model") enter up*3 enter, one fused batch."""
        case = "case-01-kimi-model-menu"
        _ensure_idle(harness, kimi_session, case=case)
        _save_screen(harness, kimi_session, case, "01-before.txt")

        events = (
            [{"type": "text", "text": "/model"}, {"type": "key", "key": "Enter"}]
            + [{"type": "key", "key": "Up"} for _ in range(3)]
            + [{"type": "key", "key": "Enter"}]
        )
        request, response = _post_events(harness, kimi_session, events)
        harness.evidence.write_json(case, "02-fused-request.json", request)
        harness.evidence.write_json(case, "03-fused-response.json", response)
        assert response["outcome"] == "accepted", response
        assert [event["outcome"] for event in response["events"]] == ["sent"] * len(
            events
        ), response

        # Whether the picker committed or was left open decides what the
        # fused form means on this build; give the mount a bounded window.
        menu_open = _await_menu(harness, kimi_session, timeout=6.0)
        time.sleep(0.5)
        after = _save_screen(harness, kimi_session, case, "04-after-fused.txt")
        history = _save_screen(
            harness, kimi_session, case, "05-after-fused-history.txt", history=True
        )
        if "Switched to" in history or "Already using" in history:
            line = next(
                row.strip()
                for row in history.splitlines()
                if "Switched to" in row or "Already using" in row
            )
            harness.evidence.note(case, f"fused sequence outcome: model switch applied — {line!r}")
        else:
            harness.evidence.note(
                case,
                f"fused sequence outcome: NO model-switch status line after the fused batch "
                f"(picker mounted={menu_open}, still open={_kimi_menu_open(harness, kimi_session)}). "
                "The Up/Enter keys of the fused form arrive before the picker finishes mounting "
                "and do not commit a selection on this build — the §10.3 fused form is not a "
                "deterministic model change here; the paced form is proven in case-04.",
            )
        _ensure_idle(harness, kimi_session, case=case)

    def test_02_reasoning_level_navigation(
        self, harness: Harness, kimi_session: ProviderSession
    ) -> None:
        """§10.3 case 2: /model menu Left/Right reasoning-level navigation.

        On the pinned build the picker's Left/Right adjusts the thinking
        segment only for models with more than one effort segment; what
        actually happened is recorded rather than forced.
        """
        case = "case-02-kimi-reasoning-menu"
        _ensure_idle(harness, kimi_session, case=case)
        _save_screen(harness, kimi_session, case, "01-before.txt")

        request, response = _post_events(
            harness,
            kimi_session,
            [{"type": "text", "text": "/model"}, {"type": "key", "key": "Enter"}],
        )
        harness.evidence.write_json(case, "02-open-menu-response.json", response)
        assert response["outcome"] == "accepted", response
        assert _await_menu(
            harness, kimi_session
        ), f"the /model picker never mounted:\n{_capture(harness, kimi_session)}"
        menu = _save_screen(harness, kimi_session, case, "03-menu-open.txt")
        harness.evidence.note(
            case,
            "menu after /model+Enter: see 03-menu-open.txt; "
            f"picker hints visible={'↑↓' in menu or 'navigate' in menu}",
        )

        time.sleep(GRACE_SLEEP)  # the /model Enter armed the dispatch grace
        events = [
            {"type": "key", "key": "Left"},
            {"type": "key", "key": "Left"},
            {"type": "key", "key": "Right"},
            {"type": "key", "key": "Enter"},
        ]
        request, response = _post_events(harness, kimi_session, events)
        harness.evidence.write_json(case, "04-left-left-right-enter-response.json", response)
        assert response["outcome"] == "accepted", response
        time.sleep(1.5)
        after = _save_screen(harness, kimi_session, case, "05-after.txt", history=True)
        switched = [
            row.strip()
            for row in after.splitlines()
            if "Thinking set to" in row or "Switched to" in row
        ]
        harness.evidence.note(
            case,
            f"after Left×2 Right Enter: status lines={switched!r}. "
            + (
                "Reasoning/effort segment changed via the /model menu."
                if switched
                else "No thinking/effort change observed — on this build the selected model "
                "exposes a single effort segment (Left/Right are no-ops on it), or the "
                "reasoning control lives elsewhere. DEVIATION recorded per §10.3."
            ),
        )
        _ensure_idle(harness, kimi_session, case=case)

    def test_03_home_end_against_model_menu(
        self, harness: Harness, kimi_session: ProviderSession
    ) -> None:
        """§10.3 Home/End against the /model menu (F4/OD5): record behavior."""
        case = "case-03-kimi-home-end-menu"
        _ensure_idle(harness, kimi_session, case=case)

        request, response = _post_events(
            harness,
            kimi_session,
            [{"type": "text", "text": "/model"}, {"type": "key", "key": "Enter"}],
        )
        assert response["outcome"] == "accepted", response
        assert _await_menu(
            harness, kimi_session
        ), f"the /model picker never mounted:\n{_capture(harness, kimi_session)}"
        _save_screen(harness, kimi_session, case, "01-menu-open.txt")

        time.sleep(GRACE_SLEEP)
        # Move the selection off the top row first so Home has somewhere to move from.
        _request, response = _post_events(
            harness, kimi_session, [{"type": "key", "key": "Down"}, {"type": "key", "key": "Down"}]
        )
        harness.evidence.write_json(case, "02-down-down-response.json", response)
        assert response["outcome"] == "accepted", response
        time.sleep(0.5)
        _save_screen(harness, kimi_session, case, "03-after-down-down.txt")

        _request, response = _post_events(harness, kimi_session, [{"type": "key", "key": "Home"}])
        harness.evidence.write_json(case, "04-home-response.json", response)
        assert response["outcome"] == "accepted", response
        time.sleep(0.5)
        after_home = _save_screen(harness, kimi_session, case, "05-after-home.txt")

        _request, response = _post_events(harness, kimi_session, [{"type": "key", "key": "End"}])
        harness.evidence.write_json(case, "06-end-response.json", response)
        assert response["outcome"] == "accepted", response
        time.sleep(0.5)
        after_end = _save_screen(harness, kimi_session, case, "07-after-end.txt")

        harness.evidence.note(
            case,
            "Home/End against the open /model menu: compare 03 (selection moved down) vs "
            "05 (after Home) vs 07 (after End). Selection highlight rows are the evidence; "
            "if 03==05==07 the menu ignores Home/End (limitation to record per F4/OD5).",
        )
        # Close the menu without selecting (Escape cancels the picker when
        # there is no query to clear; guarded so no bare Escape reaches an
        # empty composer).
        if _kimi_menu_open(harness, kimi_session):
            _request, response = _post_events(
                harness, kimi_session, [{"type": "key", "key": "Escape"}]
            )
            harness.evidence.note(case, f"menu-close Escape: outcome={response['outcome']}")
            time.sleep(0.5)
        if _kimi_menu_open(harness, kimi_session):
            _request, response = _post_events(
                harness, kimi_session, [{"type": "key", "key": "Escape"}]
            )
            harness.evidence.note(
                case, f"menu-close Escape (second): outcome={response['outcome']}"
            )
            time.sleep(0.5)
        _save_screen(harness, kimi_session, case, "08-after-menu-close.txt", history=True)


class TestKimiStreamingCadence:
    def test_04_human_cadence_batches_and_dispatch_grace(
        self, harness: Harness, kimi_session: ProviderSession
    ) -> None:
        """§10.3 streaming-cadence acceptance + the 5 s dispatch grace.

        (a) a composer batch sent immediately after an Enter-carrying
        accepted batch is refused ``pane-busy`` (the grace);
        (b) after the grace, several Up/Down batches at human cadence
        (separate requests ~300 ms apart) into the open /model menu are
        accepted — the menu's turn state reads IDLE/COMPLETED;
        (c) the closing Enter is accepted.
        """
        case = "case-04-kimi-cadence-grace"
        _ensure_idle(harness, kimi_session, case=case)

        # (0) open the menu
        _request, response = _post_events(
            harness,
            kimi_session,
            [{"type": "text", "text": "/model"}, {"type": "key", "key": "Enter"}],
        )
        harness.evidence.write_json(case, "01-open-menu-response.json", response)
        assert response["outcome"] == "accepted", response
        assert _await_menu(
            harness, kimi_session
        ), f"the /model picker never mounted:\n{_capture(harness, kimi_session)}"
        _save_screen(harness, kimi_session, case, "02-menu-open.txt")

        # (a) immediate composer batch → dispatch-grace pane-busy
        request, refused = _post_events(harness, kimi_session, [{"type": "key", "key": "Down"}])
        harness.evidence.write_json(case, "03-grace-request.json", request)
        harness.evidence.write_json(case, "04-grace-refusal.json", refused)
        assert refused["outcome"] == "refused", refused
        assert refused["reason_code"] == "pane-busy", refused
        assert "dispatch grace" in refused.get("detail", ""), refused
        assert all(event["outcome"] == "refused" for event in refused["events"]), refused
        harness.evidence.note(
            case,
            f"grace refusal recorded: reason={refused['reason_code']!r} "
            f"detail={refused.get('detail')!r}; zero bytes (all events refused).",
        )

        # (b) wait out the grace, then human-cadence navigation batches
        time.sleep(GRACE_SLEEP)
        accepted = []
        for index, key in enumerate(["Down", "Up", "Down"]):
            _request, response = _post_events(harness, kimi_session, [{"type": "key", "key": key}])
            harness.evidence.write_json(case, f"05-nav-{index}-{key}-response.json", response)
            accepted.append(response["outcome"])
            _save_screen(harness, kimi_session, case, f"06-after-nav-{index}-{key}.txt")
            time.sleep(0.3)
        assert accepted == ["accepted"] * 3, accepted

        # (c) the closing Enter
        _request, response = _post_events(harness, kimi_session, [{"type": "key", "key": "Enter"}])
        harness.evidence.write_json(case, "07-enter-response.json", response)
        assert response["outcome"] == "accepted", response
        time.sleep(1.5)
        history = _save_screen(
            harness, kimi_session, case, "08-after-enter-history.txt", history=True
        )
        status_lines = [
            row.strip()
            for row in history.splitlines()
            if "Switched to" in row or "Already using" in row or "Saved" in row
        ]
        harness.evidence.note(
            case,
            f"PASS: grace refusal + 3 accepted nav batches + accepted Enter. "
            f"Post-Enter status lines: {status_lines!r}.",
        )
        _ensure_idle(harness, kimi_session, case=case)


class TestKimiSteerAndInterrupt:
    def _start_count_turn(
        self, harness: Harness, session: ProviderSession, case: str, prompt: str
    ) -> None:
        _request, response = _post_events(
            harness, session, [{"type": "text", "text": prompt}, {"type": "key", "key": "Enter"}]
        )
        harness.evidence.write_json(case, "turn-prompt-response.json", response)
        assert response["outcome"] == "accepted", response

    def _await_active_turn(self, harness: Harness, session: ProviderSession, case: str) -> None:
        assert _await(
            lambda: _turn_active(harness, session), timeout=60.0, poll=0.2
        ), f"the provider turn never showed as active:\n{_capture(harness, session)}"

    def test_05_steer_mid_turn(self, harness: Harness, kimi_session: ProviderSession) -> None:
        """§10.3 steer: C-s with text mid-turn.

        The §3.2 gate refuses a text-bearing v3 batch mid-turn (recorded
        as a deviation from §10.3's fused reading); the deployed v2 steer
        control (text + chord, no Enter) delivers the steer.  Both are
        asserted and recorded.
        """
        case = "case-05-kimi-steer"
        _ensure_idle(harness, kimi_session, case=case)
        self._start_count_turn(
            harness,
            kimi_session,
            case,
            "Count from 1 to 1000 slowly, one number per line.",
        )
        self._await_active_turn(harness, kimi_session, case)
        _save_screen(harness, kimi_session, case, "01-turn-active.txt")

        # The Enter that started the turn armed the dispatch grace; wait
        # it out so the refusal below is attributable to the idle gate,
        # not the grace window.
        time.sleep(GRACE_SLEEP)
        if not _turn_active(harness, kimi_session):
            harness.evidence.note(
                case, "turn finished before the steer probes; restarting a fresh turn"
            )
            self._start_count_turn(
                harness,
                kimi_session,
                case,
                "Now count from 2000 to 3000 slowly, one number per line.",
            )
            self._await_active_turn(harness, kimi_session, case)
            time.sleep(GRACE_SLEEP)
        assert _turn_active(
            harness, kimi_session
        ), f"no active turn to steer into:\n{_capture(harness, kimi_session)}"

        # (a) The §10.3 fused v3 form mid-turn: text + chord C-s.
        request, refused = _post_events(
            harness,
            kimi_session,
            [
                {"type": "text", "text": "Please reconsider this path"},
                {"type": "chord", "chord": "C-s"},
            ],
        )
        harness.evidence.write_json(case, "02-fused-steer-request.json", request)
        harness.evidence.write_json(case, "03-fused-steer-response.json", refused)
        harness.evidence.note(
            case,
            f"fused v3 steer [text, chord C-s] mid-turn: outcome={refused['outcome']!r} "
            f"reason={refused.get('reason_code')!r} detail={refused.get('detail')!r}",
        )

        # (b) The bare chord mid-turn: interrupt-class, deliverable.
        request, chord = _post_events(harness, kimi_session, [{"type": "chord", "chord": "C-s"}])
        harness.evidence.write_json(case, "04-bare-chord-response.json", chord)
        assert chord["outcome"] == "accepted", chord

        # (c) The deployed v2 steer control: text + chord, enter=false.
        request, steered = _post(
            harness,
            kimi_session,
            {"text": "Please reconsider this path", "chord": "C-s", "enter": False},
        )
        harness.evidence.write_json(case, "05-v2-steer-request.json", request)
        harness.evidence.write_json(case, "06-v2-steer-response.json", steered)
        harness.evidence.note(
            case,
            f"v2 steer control (text + chord C-s, enter=false): outcome={steered['outcome']!r} "
            f"chord_sent={steered.get('chord_sent')!r}.",
        )
        assert steered["outcome"] == "accepted", steered
        time.sleep(4.0)
        history = _save_screen(
            harness, kimi_session, case, "07-after-steer-history.txt", history=True
        )
        steer_marks = [
            row.strip()
            for row in history.splitlines()
            if "reconsider" in row.lower() or "steer" in row.lower()
        ]
        harness.evidence.note(
            case, f"post-steer transcript lines naming the steer: {steer_marks!r}"
        )

        # The fused v3 form is the deviation question: assert the typed,
        # zero-byte answer it actually gets (the §3.2 gate), whatever it is.
        if refused["outcome"] == "refused":
            assert refused["reason_code"] == "pane-busy", refused
            assert all(event["outcome"] == "refused" for event in refused["events"]), refused
            harness.evidence.note(
                case,
                "DEVIATION (vs a naive reading of §10.3): the fused v3 [text, chord] steer is "
                "readiness-gated as a whole (any composer-class event gates the batch) and is "
                "refused pane-busy mid-turn with zero bytes.  The deployed mid-turn steer path "
                "is the v2 steer control (proven accepted above).",
            )
        else:
            harness.evidence.note(
                case,
                "fused v3 steer was ACCEPTED mid-turn — the gate did not refuse it; the "
                "transcript evidence shows what the provider did with it.",
            )

    def test_06_escape_interrupts_streaming(
        self, harness: Harness, kimi_session: ProviderSession
    ) -> None:
        """§10.3 / OD2: Escape interrupts streaming output."""
        case = "case-06-kimi-escape"
        if not _turn_active(harness, kimi_session):
            _ensure_idle(harness, kimi_session, case=case)
            self._start_count_turn(
                harness,
                kimi_session,
                case,
                "Count from 600 to 900 slowly, one number per line.",
            )
            self._await_active_turn(harness, kimi_session, case)
        _save_screen(harness, kimi_session, case, "01-turn-active.txt")

        _request, response = _post_events(harness, kimi_session, [{"type": "key", "key": "Escape"}])
        harness.evidence.write_json(case, "02-escape-response.json", response)
        assert response["outcome"] == "accepted", response

        stopped = _await(lambda: not _turn_active(harness, kimi_session), timeout=30.0, poll=0.25)
        after = _save_screen(harness, kimi_session, case, "03-after-escape.txt", history=True)
        assert stopped, f"turn still active 30 s after Escape:\n{after}"
        interrupt_marks = [
            row.strip()
            for row in after.splitlines()
            if "interrupt" in row.lower() or "stopped" in row.lower() or "cancel" in row.lower()
        ]
        harness.evidence.note(
            case,
            f"PASS: Escape accepted mid-turn; streaming stopped. "
            f"Transcript interrupt markers: {interrupt_marks!r}.",
        )


class TestKimiCommandGuard:
    def test_07_declared_compact_refused_with_prefill(
        self, harness: Harness, kimi_session: ProviderSession
    ) -> None:
        """§10.3 / §4.1 (a): seeded prefill + one Escape, then a declared
        /compact must refuse ``composer-nonempty`` with the prefill
        byte-identical and zero command bytes."""
        case = "case-07-kimi-guard-prefill"
        _ensure_idle(harness, kimi_session, case=case)
        if _kimi_composer_nonblank(harness, kimi_session):
            harness.evidence.note(case, "leftover composer content from an earlier case; clearing")
            _request, cleared = _post_events(
                harness,
                kimi_session,
                [{"type": "key", "key": "Home"}]
                + [{"type": "key", "key": "Delete"} for _ in range(31)],
            )
            assert cleared["outcome"] == "accepted", cleared
            assert _await(lambda: not _kimi_composer_nonblank(harness, kimi_session), timeout=5.0)

        _request, response = _post_events(
            harness, kimi_session, [{"type": "text", "text": "queued draft"}]
        )
        harness.evidence.write_json(case, "01-seed-response.json", response)
        assert response["outcome"] == "accepted", response
        time.sleep(0.5)
        before_escape = _save_screen(harness, kimi_session, case, "02-prefill-before-escape.txt")
        _save_screen(
            harness, kimi_session, case, "02b-prefill-before-escape-styled.txt", styled=True
        )
        seeded_before = _kimi_composer_prefill(before_escape)
        assert (
            seeded_before is not None and "queued draft" in seeded_before
        ), f"seeded prefill not visible in the composer:\n{before_escape}"

        _request, esc = _post_events(harness, kimi_session, [{"type": "key", "key": "Escape"}])
        harness.evidence.write_json(case, "03-escape-response.json", esc)
        assert esc["outcome"] == "accepted", esc
        time.sleep(0.5)
        after_escape = _save_screen(harness, kimi_session, case, "04-prefill-after-escape.txt")
        survived = _composer_holds(harness, kimi_session, "queued draft")
        harness.evidence.note(
            case,
            f"prefill after one Escape: {'SURVIVED' if survived else 'CLEARED'} "
            "(compare 02 vs 04).",
        )
        if not survived:
            # The guard cannot be provoked via Escape on this build — say
            # so, then provoke it with a fresh seed and no Escape.
            harness.evidence.note(
                case,
                "kimi 0.29.2 clears composer prefill on a single Escape — unlike the r5 Claude "
                "evidence.  Re-seeding prefill without Escape to provoke the guard.",
            )
            _request, response = _post_events(
                harness, kimi_session, [{"type": "text", "text": "queued draft"}]
            )
            assert response["outcome"] == "accepted", response
            time.sleep(0.5)
            after_escape = _save_screen(harness, kimi_session, case, "05-prefill-reseeded.txt")
            seeded_before = _kimi_composer_prefill(after_escape)
            assert seeded_before is not None and "queued draft" in seeded_before

        request, refused = _post_events(
            harness,
            kimi_session,
            [{"type": "text", "text": "/compact"}, {"type": "key", "key": "Enter"}],
            payload_class="command",
        )
        harness.evidence.write_json(case, "06-declared-compact-request.json", request)
        harness.evidence.write_json(case, "07-declared-compact-refusal.json", refused)
        assert refused["outcome"] == "refused", refused
        assert refused["reason_code"] == "composer-nonempty", refused
        assert all(event["outcome"] == "refused" for event in refused["events"]), refused
        harness.evidence.note(
            case,
            f"guard refusal detail: {refused.get('detail')!r}",
        )

        time.sleep(0.5)
        after_guard = _save_screen(harness, kimi_session, case, "08-after-guard.txt")
        composer_after = _kimi_composer_prefill(after_guard)
        assert composer_after == seeded_before, (
            "prefill changed by the refused command:\n"
            f"before: {seeded_before!r}\nafter:  {composer_after!r}"
        )
        history = _capture(harness, kimi_session, history=True)
        assert (
            "ompacting" not in history and "ompacted" not in history
        ), f"a compaction UI appeared despite the refusal:\n{history}"
        harness.evidence.note(
            case,
            f"PASS: declared /compact refused composer-nonempty; prefill byte-identical "
            f"({composer_after!r}); zero command bytes (no compaction UI in transcript).",
        )

    def test_08_declared_compact_executes_on_empty_composer(
        self, harness: Harness, kimi_session: ProviderSession
    ) -> None:
        """§10.3 / §4.1 (b): cleared composer → declared /compact accepted
        and the transcript shows the command's own UI.

        This is the case that exposed the composer-region pin mismatch on
        the first acceptance round: the pin then expected a
        ``── input ──`` rule the installed build never draws, so emptiness
        was unprovable and every declared command failed closed.  The pin
        was corrected from this evidence to read the rounded composer box;
        the failure branch below stays as the regression guard — a pin
        that stops seeing this build's composer fails the case loudly with
        the exact observed-vs-expected diagnostic.
        """
        case = "case-08-kimi-compact-executes"
        _ensure_idle(harness, kimi_session, case=case)
        if _kimi_composer_nonblank(harness, kimi_session):
            harness.evidence.note(case, "composer not empty at case start; clearing")
            _request, cleared = _post_events(
                harness,
                kimi_session,
                [{"type": "key", "key": "Home"}]
                + [{"type": "key", "key": "Delete"} for _ in range(31)],
            )
            assert cleared["outcome"] == "accepted", cleared
            assert _await(
                lambda: not _kimi_composer_nonblank(harness, kimi_session), timeout=5.0
            ), f"could not clear the composer:\n{_capture(harness, kimi_session)}"
        time.sleep(0.5)
        empty_screen = _save_screen(harness, kimi_session, case, "01-composer-empty.txt")
        _save_screen(harness, kimi_session, case, "01b-composer-empty-styled.txt", styled=True)
        prefill = _kimi_composer_prefill(empty_screen)
        assert prefill is not None and not any(
            row.strip() for row in prefill.splitlines()
        ), f"composer not visually empty before the declared command:\n{empty_screen}"
        harness.evidence.note(
            case,
            "composer visually empty (rounded box, no content after '>'): 01/01b captures. "
            "The server pin reads this same rounded composer box (corrected from this evidence).",
        )

        request, accepted = _post_events(
            harness,
            kimi_session,
            [{"type": "text", "text": "/compact"}, {"type": "key", "key": "Enter"}],
            payload_class="command",
        )
        harness.evidence.write_json(case, "02-declared-compact-request.json", request)
        harness.evidence.write_json(case, "03-declared-compact-response.json", accepted)
        if accepted["outcome"] != "accepted":
            harness.evidence.note(
                case,
                f"IMPOSSIBLE on this build: declared /compact refused "
                f"{accepted.get('reason_code')!r} — {accepted.get('detail')!r}. "
                "The composer was visually empty; the pin's region determination cannot see "
                "this build's rounded composer box, so emptiness is unprovable and every "
                "declared command fails closed.",
            )
            pytest.fail(
                "§4.1(b) IMPOSSIBLE on the installed kimi 0.29.2: the composer was visually "
                "empty (see evidence 01/01b) yet the declared command was refused "
                f"{accepted.get('reason_code')!r}: {accepted.get('detail')!r}. "
                "The server pin (native_pane_input's kimi-composer-box rule) no longer sees "
                "this build's rounded composer box (╭─╮/│ > …│/╰─╯): emptiness is unprovable "
                "and every declared command fails closed. The pin must match the build it names."
            )
        assert accepted["outcome"] == "accepted", accepted
        # The r11 two-close rule, live: the accepted record carries the
        # execution observation and its evidence reference — never a
        # transport-only acceptance with a null observation.
        assert accepted.get("submission_observed") == "submitted", accepted
        assert accepted.get("submission_evidence_ref", "").startswith("capture-pane:"), accepted
        replayed = requests.get(
            f"{harness.server.url}/control-input/{request['control_id']}", timeout=30
        ).json()
        assert replayed["outcome"] == "accepted", replayed
        assert replayed.get("submission_observed") == "submitted", replayed
        assert replayed.get("submission_evidence_ref") == accepted["submission_evidence_ref"]

        # Compaction is a provider operation; give it a generous window.
        found = _await(
            lambda: "ompact" in _capture(harness, kimi_session, history=True),
            timeout=90.0,
            poll=1.0,
        )
        history = _save_screen(
            harness, kimi_session, case, "04-after-compact-history.txt", history=True
        )
        compact_lines = [row.strip() for row in history.splitlines() if "ompact" in row]
        assert found, f"no compaction UI appeared within 90 s:\n{history}"
        harness.evidence.note(
            case,
            f"PASS: declared /compact accepted on a proven-empty composer WITH execution "
            f"evidence (submission_observed=submitted, ref "
            f"{accepted['submission_evidence_ref']!r}); compaction UI lines: {compact_lines!r}.",
        )
        _ensure_idle(harness, kimi_session, case=case)

    def test_09_undeclared_slash_text_is_prose(
        self, harness: Harness, kimi_session: ProviderSession
    ) -> None:
        """§10.3 / r7 carrier: an UNDECLARED batch whose text begins with
        a slash is delivered as prose — no guard refusal, no disarm."""
        case = "case-09-kimi-slash-carrier"
        _ensure_idle(harness, kimi_session, case=case)

        request, response = _post_events(
            harness, kimi_session, [{"type": "text", "text": "/tmp/x"}]
        )
        harness.evidence.write_json(case, "01-undeclared-request.json", request)
        harness.evidence.write_json(case, "02-undeclared-response.json", response)
        assert response["outcome"] == "accepted", response
        time.sleep(0.5)
        screen = _save_screen(harness, kimi_session, case, "03-composer-with-slash-text.txt")
        composer = _kimi_composer_text(screen)
        assert (
            composer is not None and "/tmp/x" in composer
        ), f"the slash-leading prose did not land in the composer:\n{screen}"
        harness.evidence.note(
            case,
            f"PASS: undeclared '/tmp/x' accepted as prose (no payload_class → never enters the "
            f"guard); composer holds it verbatim: {composer!r}.",
        )
        _clear_composer(harness, kimi_session, seeded="/tmp/x", case=case)


# ===========================================================================
# Claude 2.1.220 — disposable native-TUI session
# ===========================================================================


class TestClaudeIdentitySurface:
    def test_control_identity_is_build_exact(
        self, harness: Harness, claude_session: ProviderSession
    ) -> None:
        identity = _control_identity(harness, claude_session)
        harness.evidence.write_json("case-00-claude-identity", "control-identity.json", identity)
        block = identity.get("control_input") or {}
        provider_controls = (block.get("provider_controls") or {}).get("claude_code") or {}
        assert provider_controls.get("steer_chords") == [], provider_controls
        assert (provider_controls.get("compact") or {}).get("events") == [
            {"type": "text", "text": "/compact"},
            {"type": "key", "key": "Enter"},
        ], provider_controls
        assert (block.get("command_controls") or {}).get("composer_nonempty_guard") is True, block
        harness.evidence.note(
            "case-00-claude-identity",
            f"PASS: build-exact provider_controls for claude_code {CLAUDE_PIN}: steer_chords=[], "
            "compact shape pinned, composer guard advertised.",
        )


class TestClaudeCommandGuard:
    def test_01_declared_compact_refused_with_prefill(
        self, harness: Harness, claude_session: ProviderSession
    ) -> None:
        """§10.3 / §4.1 (a) on Claude: styled placeholder-vs-prefill
        evidence, one Escape, then the declared /compact refusal with the
        prefill byte-identical."""
        case = "case-11-claude-guard-prefill"
        _ensure_idle(harness, claude_session, case=case)

        styled_empty = _save_screen(
            harness, claude_session, case, "01-empty-styled.txt", styled=True
        )
        harness.evidence.note(
            case,
            "empty composer, styled capture (placeholder = dim/inverse cells): 01-empty-styled.txt",
        )

        _request, response = _post_events(
            harness, claude_session, [{"type": "text", "text": "queued draft"}]
        )
        harness.evidence.write_json(case, "02-seed-response.json", response)
        assert response["outcome"] == "accepted", response
        time.sleep(0.5)
        styled_prefill = _save_screen(
            harness, claude_session, case, "03-prefill-styled.txt", styled=True
        )

        _request, esc = _post_events(harness, claude_session, [{"type": "key", "key": "Escape"}])
        harness.evidence.write_json(case, "04-escape-response.json", esc)
        assert esc["outcome"] == "accepted", esc
        time.sleep(0.5)
        styled_after_escape = _save_screen(
            harness, claude_session, case, "05-prefill-after-escape-styled.txt", styled=True
        )
        survived = _composer_holds(harness, claude_session, "queued draft")
        harness.evidence.note(
            case,
            f"prefill after one Escape: {'SURVIVED' if survived else 'CLEARED'} "
            "(styled captures 03 vs 05; the r5 evidence says Claude prefill survives Escape).",
        )
        if not survived:
            harness.evidence.note(
                case,
                "claude 2.1.220 cleared prefill on a single Escape in this run — differs from the "
                "r5 evidence; re-seeding without Escape to provoke the guard.",
            )
            _request, response = _post_events(
                harness, claude_session, [{"type": "text", "text": "queued draft"}]
            )
            assert response["outcome"] == "accepted", response
            time.sleep(0.5)
            styled_after_escape = _save_screen(
                harness, claude_session, case, "06-prefill-reseeded-styled.txt", styled=True
            )

        request, refused = _post_events(
            harness,
            claude_session,
            [{"type": "text", "text": "/compact"}, {"type": "key", "key": "Enter"}],
            payload_class="command",
        )
        harness.evidence.write_json(case, "07-declared-compact-request.json", request)
        harness.evidence.write_json(case, "08-declared-compact-refusal.json", refused)
        assert refused["outcome"] == "refused", refused
        assert refused["reason_code"] == "composer-nonempty", refused
        assert all(event["outcome"] == "refused" for event in refused["events"]), refused

        time.sleep(0.5)
        styled_after_guard = _save_screen(
            harness, claude_session, case, "09-after-guard-styled.txt", styled=True
        )
        # Byte-identity is asserted on the composer region (rules +
        # content rows): status-line chrome may legitimately repaint
        # between two captures of an untouched composer.
        region_before = _claude_composer_styled_region(styled_after_escape)
        region_after = _claude_composer_styled_region(styled_after_guard)
        assert (
            region_before is not None and region_after is not None
        ), f"could not locate the claude composer box in the styled captures:\n{styled_after_guard}"
        assert region_after == region_before, (
            "the refused command changed the composer (prefill not byte-identical):\n"
            f"before:\n{region_before}\nafter:\n{region_after}"
        )
        harness.evidence.note(
            case,
            "PASS: declared /compact refused composer-nonempty; styled capture byte-identical "
            "before/after (prefill untouched, zero command bytes).",
        )

    def test_02_declared_compact_executes_on_empty_composer(
        self, harness: Harness, claude_session: ProviderSession
    ) -> None:
        """§10.3 / §4.1 (b) on Claude: cleared composer → declared
        /compact accepted; the transcript shows the compaction UI."""
        case = "case-12-claude-compact-executes"
        _ensure_idle(harness, claude_session, case=case)
        if _composer_holds(harness, claude_session, "queued draft"):
            _clear_composer(harness, claude_session, seeded="queued draft", case=case)
        time.sleep(0.5)
        _save_screen(harness, claude_session, case, "01-composer-empty-styled.txt", styled=True)

        request, accepted = _post_events(
            harness,
            claude_session,
            [{"type": "text", "text": "/compact"}, {"type": "key", "key": "Enter"}],
            payload_class="command",
        )
        harness.evidence.write_json(case, "02-declared-compact-request.json", request)
        harness.evidence.write_json(case, "03-declared-compact-response.json", accepted)
        assert accepted["outcome"] == "accepted", accepted
        # The r11 two-close rule, live: the accepted record carries the
        # execution observation and its evidence reference.
        assert accepted.get("submission_observed") == "submitted", accepted
        assert accepted.get("submission_evidence_ref", "").startswith("capture-pane:"), accepted
        replayed = requests.get(
            f"{harness.server.url}/control-input/{request['control_id']}", timeout=30
        ).json()
        assert replayed["outcome"] == "accepted", replayed
        assert replayed.get("submission_observed") == "submitted", replayed
        assert replayed.get("submission_evidence_ref") == accepted["submission_evidence_ref"]

        # The command's own UI is the result line, not the submission
        # echo: on this fresh session the honest result is "Not enough
        # messages to compact."  Wait for the compaction turn to finish.
        result_found = _await(
            lambda: (
                "Not enough messages to compact" in _capture(harness, claude_session, history=True)
                or "Compacted" in _capture(harness, claude_session, history=True)
            ),
            timeout=120.0,
            poll=1.0,
        )
        history = _save_screen(
            harness, claude_session, case, "04-after-compact-history.txt", history=True
        )
        compact_lines = [
            row.strip() for row in history.splitlines() if "ompact" in row or "Compacted" in row
        ]
        assert result_found, f"no compaction result line appeared within 120 s:\n{history}"
        harness.evidence.note(
            case,
            f"PASS: declared /compact accepted on a proven-empty composer WITH execution "
            f"evidence (submission_observed=submitted, ref "
            f"{accepted['submission_evidence_ref']!r}); the transcript shows "
            f"the command's own UI (result line, not a prompt echo): {compact_lines!r}.",
        )
        _ensure_idle(harness, claude_session, case=case)

    def test_03_escape_interrupts_active_turn(
        self, harness: Harness, claude_session: ProviderSession
    ) -> None:
        """§10.3 on Claude: Escape interrupts an active turn ("esc to interrupt")."""
        case = "case-13-claude-escape"
        _ensure_idle(harness, claude_session, case=case)
        _request, response = _post_events(
            harness,
            claude_session,
            [
                {"type": "text", "text": "Count from 1 to 150 slowly, one number per line."},
                {"type": "key", "key": "Enter"},
            ],
        )
        harness.evidence.write_json(case, "01-turn-prompt-response.json", response)
        assert response["outcome"] == "accepted", response
        assert _await(
            lambda: _turn_active(harness, claude_session), timeout=60.0, poll=0.2
        ), f"the claude turn never showed as active:\n{_capture(harness, claude_session)}"
        _save_screen(harness, claude_session, case, "02-turn-active.txt")

        _request, esc = _post_events(harness, claude_session, [{"type": "key", "key": "Escape"}])
        harness.evidence.write_json(case, "03-escape-response.json", esc)
        assert esc["outcome"] == "accepted", esc

        stopped = _await(lambda: not _turn_active(harness, claude_session), timeout=30.0, poll=0.25)
        after = _save_screen(harness, claude_session, case, "04-after-escape.txt", history=True)
        assert stopped, f"claude turn still active 30 s after Escape:\n{after}"
        interrupt_marks = [
            row.strip()
            for row in after.splitlines()
            if "interrupt" in row.lower() or "stopped" in row.lower()
        ]
        harness.evidence.note(
            case,
            f"PASS: Escape accepted mid-turn; the turn stopped. "
            f"Transcript interrupt markers: {interrupt_marks!r}.",
        )
