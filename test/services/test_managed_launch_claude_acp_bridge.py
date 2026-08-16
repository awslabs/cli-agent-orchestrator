"""Claude Code ACP bridge adapter: wrapper/inner route envelopes (COND-0415).

The managed DeepSeek route never relies on ambient server credentials or
PATH: the reservation pins the real Claude binary as ``provider_executable``
(the version probe runs exactly it and must leave the one-shot token
present), and the provider session launches the envelope-pinned wrapper
exactly once under the bounded conductor route environment, so only that
process claims the token and records the consumed marker.  Readiness is
published only after the SessionStart hook (exact session proof) AND the
provider's own system/init event attest the session, model, and working
directory — and, for DeepSeek, after the wrapper-consumed marker exists.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import signal
import threading
import time
import uuid
from typing import Any, Optional

import pytest

from cli_agent_orchestrator.services import deepseek_acp_route, managed_launch
from cli_agent_orchestrator.services import managed_provider_bridge as bridge
from cli_agent_orchestrator.services import provider_contracts
from cli_agent_orchestrator.services.managed_event_renderer import ManagedEventRenderer


def _write_executable(path: pathlib.Path, body: str) -> str:
    path.write_text(body)
    path.chmod(0o755)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _deepseek_envelope(tmp_path: pathlib.Path, *, model: str = "deepseek-v4-flash") -> dict:
    """A complete deepseek envelope plus its wrapper/inner/route-map/token files."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    wrapper = tmp_path / "shims" / "claude"
    wrapper.parent.mkdir()
    wrapper_digest = _write_executable(
        wrapper, '#!/bin/sh\nexec "$CAO_CONDUCTOR_REAL_CLAUDE" "$@"\n'
    )
    inner = tmp_path / "real-claude"
    inner_digest = _write_executable(inner, "#!/bin/sh\necho 'claude 2.1.233'\n")
    token = tmp_path / "deepseek-token.txt"
    token.write_text("sk-one-shot\n")
    token.chmod(0o600)
    marker = tmp_path / "deepseek-token.consumed"
    route_map = tmp_path / "deepseek-routes.json"
    route_map.write_text(
        json.dumps(
            {
                "routes": {
                    str(worktree): {
                        "route": "deepseek",
                        "model": model,
                        "token_path": str(token),
                        "consumed_path": str(marker),
                    }
                }
            }
        )
    )
    return {
        "wrapper_executable": str(wrapper),
        "wrapper_executable_sha256": wrapper_digest,
        "inner_executable": str(inner),
        "inner_executable_sha256": inner_digest,
        "route_map_path": str(route_map),
        "worktree_realpath": str(worktree),
        "token_path": str(token),
        "consumed_marker_path": str(marker),
    }


def _claude_request(
    tmp_path: pathlib.Path,
    *,
    model: str = "deepseek-v4-flash",
    effort: str = "high",
    provider_route: str = "deepseek",
    envelope: Optional[dict] = None,
    worktree: Optional[pathlib.Path] = None,
) -> dict[str, Any]:
    if provider_route == "deepseek":
        envelope = envelope if envelope is not None else _deepseek_envelope(tmp_path, model=model)
        executable = pathlib.Path(envelope["inner_executable"])
        worktree = pathlib.Path(envelope["worktree_realpath"])
    else:
        executable = tmp_path / "claude"
        executable.write_text("#!/bin/sh\necho 'claude 2.1.233'\n")
        executable.chmod(0o755)
        worktree = worktree or tmp_path
    request = {
        "bridge_version": bridge.BRIDGE_VERSION,
        "reservation_id": "11111111-1111-4111-8111-111111111111",
        "terminal_id": "deadbeef",
        "generation": "22222222-2222-4222-8222-222222222222",
        "delivery_id": "33333333-3333-4333-8333-333333333333",
        "provider": "claude_code",
        "agent_profile": "implementer",
        "profile_sha256": "a" * 64,
        "model": model,
        "effort": effort,
        "provider_route": provider_route,
        "working_directory": str(worktree),
        "provider_executable": str(executable),
        "provider_executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
    }
    if provider_route == "deepseek":
        request["route_envelope"] = envelope
    request["rendezvous_identity"] = {
        "project": "test-project",
        "task_id": "test-task",
        "terminal_id": request["terminal_id"],
        "terminal_generation": request["generation"],
        "worktree_realpath": str(worktree),
        "repository": "test-repository",
        "head": "1" * 40,
        "actor": "cafebabe",
    }
    return request


def _admission(
    request: dict[str, Any], message: str = "implement COND-0415 repair"
) -> dict[str, Any]:
    return {
        "op": "admit",
        "reservation_id": request["reservation_id"],
        "terminal_id": request["terminal_id"],
        "generation": request["generation"],
        "delivery_id": request["delivery_id"],
        "message": message,
        "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
        "sender_id": "cafebabe",
        "orchestration_type": "assign",
        "context": {"task_sha256": "b" * 64, "dossier_sha256": "c" * 64},
    }


def _material(allowed_tools: Optional[list[str]] = None) -> dict[str, Any]:
    return {
        "profile": object(),
        "profile_sha256": "a" * 64,
        "allowed_tools": ["*"] if allowed_tools is None else allowed_tools,
        "system_prompt": "implement bounded repairs accurately",
        "mcp_servers": [],
    }


class _FakeClaudeProcess:
    def __init__(
        self,
        argv: list[str],
        *,
        env: Optional[dict[str, str]] = None,
        companion_identity: Any = None,
        provider: str = "claude_code",
    ):
        self.argv = argv
        self.env = env or {}
        self.provider = provider
        self.sent_messages: list[dict[str, Any]] = []
        self._notifications: list[dict[str, Any]] = []
        self.closed = False
        self.proc = self
        self.auto_complete = True
        self.echo_user = True
        self.echo_content: Optional[str] = None
        self._condition = threading.Condition()

        # The bridge wraps the provider argv with the launcher shim
        # (python -m provider_launcher --socket ... -- <provider argv>);
        # the provider's own arguments start after the "--" separator.
        self.provider_argv = argv[argv.index("--") + 1 :] if "--" in argv else argv
        self.session_id = "test-session-uuid"
        for i, arg in enumerate(self.provider_argv):
            if arg == "--session-id" and i + 1 < len(self.provider_argv):
                self.session_id = self.provider_argv[i + 1]

        # The wrapper's claim side effect: consume the one-shot token and
        # record the marker exactly once, as the real conductor shim does.
        consume = env.pop("_fake_consume_envelope", None)
        if consume:
            token = pathlib.Path(consume["token_path"])
            if token.exists():
                token.unlink()
            pathlib.Path(consume["consumed_marker_path"]).write_text("consumed\n")

        # system/init is emitted only when the first real user message is
        # available (Claude Code 2.1.x behavior), so the fake emits it at
        # first _send, before the replayed user event.
        init = env.pop("_fake_init_event", None)
        if init is not None and init.get("session_id") == "__from_argv__":
            init = {**init, "session_id": self.session_id}
        self._init_event = init
        self._init_emitted = False

    def poll(self) -> Optional[int]:
        return None if not self.closed else 0

    def send_signal(self, sig: int) -> None:
        if sig == signal.SIGINT:
            with self._condition:
                self._notifications.append(
                    {
                        "type": "result",
                        "session_id": self.session_id,
                        "uuid": "cancelled-turn",
                        "result": "Turn interrupted by operator.",
                        "stop_reason": "cancelled",
                    }
                )
                self._condition.notify_all()

    def finish_turn(self, turn_uuid: str = "turn-uuid") -> None:
        with self._condition:
            self._notifications.append(
                {
                    "type": "result",
                    "session_id": self.session_id,
                    "uuid": turn_uuid,
                    "result": "Completed task.",
                    "stop_reason": "end_turn",
                }
            )
            self._condition.notify_all()

    def _send(self, message: dict[str, Any]) -> None:
        self.sent_messages.append(message)
        if message.get("type") == "user":
            turn_uuid = f"turn-{uuid.uuid4()}"
            with self._condition:
                if self._init_event is not None and not self._init_emitted:
                    self._init_emitted = True
                    self._notifications.append(dict(self._init_event))
                if self.echo_user:
                    self._notifications.append(
                        {
                            "type": "user",
                            "message": {
                                "role": "user",
                                "content": (
                                    self.echo_content
                                    if self.echo_content is not None
                                    else message.get("message", {}).get("content")
                                ),
                            },
                            "session_id": self.session_id,
                            "uuid": turn_uuid,
                        }
                    )
                if self.auto_complete:
                    self._notifications.append(
                        {
                            "type": "result",
                            "session_id": self.session_id,
                            "uuid": turn_uuid,
                            "result": "Completed task.",
                            "stop_reason": "end_turn",
                        }
                    )
                self._condition.notify_all()

    def notification_count(self) -> int:
        with self._condition:
            return len(self._notifications)

    def notifications_since(self, index: int) -> tuple[list[dict[str, Any]], int]:
        with self._condition:
            return list(self._notifications[index:]), len(self._notifications)

    def wait_notification(
        self, predicate: Any, *, start_index: int, timeout: float
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        with self._condition:
            index = start_index
            while True:
                while index < len(self._notifications):
                    item = self._notifications[index]
                    index += 1
                    if predicate(item):
                        return item
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise bridge.BridgeError("notification predicate timed out")
                self._condition.wait(remaining)

    def close(self) -> None:
        self.closed = True


def _deepseek_env_setup(monkeypatch: pytest.MonkeyPatch, request: dict[str, Any]) -> dict[str, Any]:
    """Bind a scrubbed bridge environment with an ambient Anthropic key present."""
    import os

    monkeypatch.setattr(
        os,
        "environ",
        {
            "HOME": str(pathlib.Path.home()),
            "PATH": "/usr/bin:/bin",
            "ANTHROPIC_API_KEY": "sk-ant-ambient-fallback",
            "ANTHROPIC_AUTH_TOKEN": "ambient-token",
            "CLAUDE_CODE_USE_BEDROCK": "",
        },
    )
    monkeypatch.setattr(bridge, "_BOUND_PROVIDER_ENV", None)
    bridge._prune_bridge_environment("claude_code")
    return request


def _session_start_hook(monkeypatch: pytest.MonkeyPatch, request: dict[str, Any]) -> None:
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.claude_native_readiness.await_session_start",
        lambda _path, session_id, **_kw: {
            "schema": "cao-claude-native-readiness-v1",
            "hook_event": "SessionStart",
            "native_session_id": session_id,
            "source": "startup",
            "cwd": request["working_directory"],
            "model": request["model"],
            "observed_session_ids": [session_id],
        },
    )


def _init_event(
    request: dict[str, Any],
    *,
    model: Optional[str] = None,
    cwd: Optional[str] = None,
    session_id: Optional[str] = None,
    emitted: bool = True,
) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
    """The fake process env keys that make the fake emit a system/init event."""
    if not emitted:
        return None, {}
    if session_id is None:
        # The fake fills in the real session id from argv at construction.
        session_id = "__from_argv__"
    return {
        "type": "system",
        "subtype": "init",
        "session_id": session_id,
        "model": request["model"] if model is None else model,
        "cwd": request["working_directory"] if cwd is None else cwd,
        "tools": ["Bash", "Read"],
    }, {}


def _build_session(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    model: str = "deepseek-v4-flash",
    provider_route: str = "deepseek",
    envelope: Optional[dict] = None,
    allowed_tools: Optional[list[str]] = None,
    init_event: Optional[dict[str, Any]] = None,
    emit_init: bool = True,
    consume: bool = True,
    echo_user: bool = True,
    echo_content: Optional[str] = None,
    version_probe: Optional[str] = None,
) -> tuple[bridge._ProviderSession, dict[str, Any], list[_FakeClaudeProcess]]:
    request = _claude_request(
        tmp_path, model=model, provider_route=provider_route, envelope=envelope
    )
    _deepseek_env_setup(monkeypatch, request)
    monkeypatch.setattr(
        bridge, "_profile_material", lambda *_: _material(allowed_tools=allowed_tools)
    )
    # The unit suite drives the uncertain paths deterministically; shrink
    # the provider wait bounds so each negative case costs milliseconds.
    monkeypatch.setattr(bridge, "_CLAUDE_INIT_TIMEOUT", 0.5)
    monkeypatch.setattr(bridge, "_CLAUDE_TURN_ACCEPT_TIMEOUT", 0.5)
    procs: list[_FakeClaudeProcess] = []

    def fake_proc(*args: Any, **kwargs: Any) -> _FakeClaudeProcess:
        env = dict(kwargs.get("env") or {})
        if init_event is not None:
            env["_fake_init_event"] = init_event
        elif emit_init:
            env["_fake_init_event"] = {
                "type": "system",
                "subtype": "init",
                "session_id": "__from_argv__",
                "model": request["model"],
                "cwd": request["working_directory"],
                "tools": ["Bash", "Read"],
            }
        if consume and provider_route == "deepseek":
            env["_fake_consume_envelope"] = request["route_envelope"]
        kwargs["env"] = env
        proc = _FakeClaudeProcess(*args, **kwargs)
        proc.echo_user = echo_user
        proc.echo_content = echo_content
        procs.append(proc)
        return proc

    monkeypatch.setattr(bridge, "_RpcProcess", fake_proc)
    if version_probe is not None:
        monkeypatch.setattr(bridge._ProviderSession, "_version", lambda *_: version_probe)
    _session_start_hook(monkeypatch, request)
    session = bridge._ProviderSession(request)
    return session, request, procs


class TestDeepSeekReadinessAndAdmission:
    def test_envelope_launch_readiness_and_first_turn(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session, request, procs = _build_session(tmp_path, monkeypatch)
        readiness = session.initialize()
        submission = session.admit(_admission(request))

        assert len(procs) == 1
        # The provider session launches the pinned wrapper exactly once.
        assert procs[0].provider_argv[0] == request["route_envelope"]["wrapper_executable"]
        # Readiness is the SessionStart hook (exact session + cwd proof)
        # plus the wrapper-consumed marker — system/init is first-turn
        # evidence on Claude Code 2.1.x, never a readiness claim.
        assert readiness["provider_receipt_kind"] == "claude-session-start"
        assert readiness["session_start"]["session_id"] == session.provider_session_id
        assert readiness["session_start"]["cwd"] == request["working_directory"]
        assert readiness["session_start"]["hook_event"] == "SessionStart"
        assert "session_init" not in readiness
        assert readiness["model_input_ready"] is True
        # The wrapper-consumed marker is required before readiness.
        assert deepseek_acp_route.consumed_marker_exists(
            request["route_envelope"]["consumed_marker_path"]
        )
        # The one-shot token was claimed exactly once, by the session.
        assert not pathlib.Path(request["route_envelope"]["token_path"]).exists()

        assert submission["provider_receipt_kind"] == "claude-turn-start"
        assert submission["provider_session_id"] == readiness["provider_session_id"]
        assert submission["provider_turn_id"] == submission["receipt_id"]
        assert submission["provider_accepted"] is True
        assert len(procs[0].sent_messages) == 1
        assert procs[0].sent_messages[0]["type"] == "user"
        # The first admission captured and validated the provider-authored
        # system/init before the replayed user event was accepted.
        assert session._claude_init is not None
        assert session._claude_init["model"] == "deepseek-v4-flash"
        assert session._claude_init["cwd"] == request["working_directory"]
        assert session._claude_init["session_id"] == session.provider_session_id

    def test_provider_child_environment_is_bounded_conductor_route(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session, request, procs = _build_session(tmp_path, monkeypatch)
        session.initialize()
        env = procs[0].env
        envelope = request["route_envelope"]
        assert env["CAO_CONDUCTOR_ROUTES"] == envelope["route_map_path"]
        assert env["CAO_CONDUCTOR_REAL_CLAUDE"] == envelope["inner_executable"]
        assert env["CAO_CONDUCTOR_SHIM_DIR"] == str(
            pathlib.Path(envelope["wrapper_executable"]).parent
        )
        assert env["PATH"].startswith(
            str(pathlib.Path(envelope["wrapper_executable"]).parent) + ":"
        )
        # No ambient Anthropic fallback: credentials and gateway pointers
        # never cross into the DeepSeek child environment.
        assert "ANTHROPIC_API_KEY" not in env
        assert "ANTHROPIC_AUTH_TOKEN" not in env
        assert "ANTHROPIC_BASE_URL" not in env
        assert env["ANTHROPIC_MODEL"] == "deepseek-v4-flash"


class TestDeepSeekFailClosedBeforeProviderIO:
    def test_missing_envelope_fails_closed_with_zero_processes(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        envelope = _deepseek_envelope(tmp_path)
        request = _claude_request(tmp_path, provider_route="deepseek", envelope=envelope)
        request.pop("route_envelope")
        _deepseek_env_setup(monkeypatch, request)
        monkeypatch.setattr(bridge, "_profile_material", lambda *_: _material())
        monkeypatch.setattr(bridge._ProviderSession, "_version", lambda *_: "claude 2.1.233")
        procs: list[_FakeClaudeProcess] = []
        monkeypatch.setattr(
            bridge, "_RpcProcess", lambda *a, **k: procs.append(_FakeClaudeProcess(*a, **k))
        )
        session = bridge._ProviderSession(request)
        with pytest.raises(bridge.BridgeError, match="requires a route_envelope"):
            session.initialize()
        assert procs == []
        # Zero task bytes: the token is untouched.
        assert pathlib.Path(envelope["token_path"]).exists()

    def test_drifted_wrapper_digest_fails_before_version_probe(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        envelope = _deepseek_envelope(tmp_path)
        envelope["wrapper_executable_sha256"] = "0" * 64
        request = _claude_request(tmp_path, envelope=envelope)
        _deepseek_env_setup(monkeypatch, request)
        monkeypatch.setattr(bridge, "_profile_material", lambda *_: _material())
        called: list[str] = []
        monkeypatch.setattr(
            bridge._ProviderSession,
            "_version",
            lambda self, *_: called.append("version") or "claude 2.1.233",
        )
        session = bridge._ProviderSession(request)
        with pytest.raises(bridge.BridgeError, match="wrapper_executable"):
            session.initialize()
        assert called == []

    def test_marker_already_present_refuses_replay(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        envelope = _deepseek_envelope(tmp_path)
        pathlib.Path(envelope["consumed_marker_path"]).write_text("consumed\n")
        request = _claude_request(tmp_path, envelope=envelope)
        _deepseek_env_setup(monkeypatch, request)
        monkeypatch.setattr(bridge, "_profile_material", lambda *_: _material())
        monkeypatch.setattr(bridge._ProviderSession, "_version", lambda *_: "claude 2.1.233")
        procs: list[_FakeClaudeProcess] = []
        monkeypatch.setattr(
            bridge, "_RpcProcess", lambda *a, **k: procs.append(_FakeClaudeProcess(*a, **k))
        )
        session = bridge._ProviderSession(request)
        with pytest.raises(bridge.BridgeError, match="consumed marker"):
            session.initialize()
        assert procs == []

    def test_missing_token_fails_closed(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        envelope = _deepseek_envelope(tmp_path)
        pathlib.Path(envelope["token_path"]).unlink()
        request = _claude_request(tmp_path, envelope=envelope)
        _deepseek_env_setup(monkeypatch, request)
        monkeypatch.setattr(bridge, "_profile_material", lambda *_: _material())
        monkeypatch.setattr(bridge._ProviderSession, "_version", lambda *_: "claude 2.1.233")
        session = bridge._ProviderSession(request)
        with pytest.raises(bridge.BridgeError, match="token"):
            session.initialize()

    def test_version_probe_consumed_token_fails_before_session_launch(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        envelope = _deepseek_envelope(tmp_path)
        request = _claude_request(tmp_path, envelope=envelope)
        _deepseek_env_setup(monkeypatch, request)
        monkeypatch.setattr(bridge, "_profile_material", lambda *_: _material())

        def consuming_probe(self: Any, *args: Any) -> str:
            pathlib.Path(envelope["token_path"]).unlink()
            return "claude 2.1.233"

        monkeypatch.setattr(bridge._ProviderSession, "_version", consuming_probe)
        procs: list[_FakeClaudeProcess] = []
        monkeypatch.setattr(
            bridge, "_RpcProcess", lambda *a, **k: procs.append(_FakeClaudeProcess(*a, **k))
        )
        session = bridge._ProviderSession(request)
        with pytest.raises(bridge.BridgeError, match="version probe"):
            session.initialize()
        assert procs == []

    def test_route_map_entry_must_match_reservation(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        envelope = _deepseek_envelope(tmp_path, model="deepseek-v4-pro")
        request = _claude_request(tmp_path, model="deepseek-v4-flash", envelope=envelope)
        _deepseek_env_setup(monkeypatch, request)
        monkeypatch.setattr(bridge, "_profile_material", lambda *_: _material())
        monkeypatch.setattr(bridge._ProviderSession, "_version", lambda *_: "claude 2.1.233")
        session = bridge._ProviderSession(request)
        with pytest.raises(bridge.BridgeError, match="route map model"):
            session.initialize()


class TestFirstTurnInitValidation:
    def test_init_wrong_session_is_uncertain_after_boundary(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        init = {
            "type": "system",
            "subtype": "init",
            "session_id": "99999999-9999-4999-8999-999999999999",
            "model": "deepseek-v4-flash",
            "cwd": str(tmp_path),
        }
        session, request, procs = _build_session(tmp_path, monkeypatch, init_event=init)
        init["cwd"] = request["working_directory"]
        session.initialize()
        with pytest.raises(bridge.SubmitUncertain, match="different provider session"):
            session.admit(_admission(request))
        # The task bytes crossed the boundary exactly once; the outcome is
        # ambiguous, never a clean refusal that invites a replay.
        assert len(procs[0].sent_messages) == 1

    def test_init_wrong_model_is_uncertain_after_boundary(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        init = {
            "type": "system",
            "subtype": "init",
            "session_id": "__from_argv__",
            "model": "deepseek-v4-pro",
            "cwd": None,
        }
        session, request, procs = _build_session(tmp_path, monkeypatch, init_event=init)
        init["cwd"] = request["working_directory"]
        session.initialize()
        with pytest.raises(bridge.SubmitUncertain, match="wrong model"):
            session.admit(_admission(request))
        assert len(procs[0].sent_messages) == 1

    def test_init_wrong_cwd_is_uncertain_after_boundary(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        init = {
            "type": "system",
            "subtype": "init",
            "session_id": "__from_argv__",
            "model": "deepseek-v4-flash",
            "cwd": "/some/other/worktree",
        }
        session, request, procs = _build_session(tmp_path, monkeypatch, init_event=init)
        session.initialize()
        with pytest.raises(bridge.SubmitUncertain, match="wrong working directory"):
            session.admit(_admission(request))
        assert len(procs[0].sent_messages) == 1

    def test_missing_init_is_uncertain_after_boundary(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session, request, procs = _build_session(tmp_path, monkeypatch, emit_init=False)
        session.initialize()
        with pytest.raises(bridge.SubmitUncertain, match="system/init"):
            session.admit(_admission(request))
        assert len(procs[0].sent_messages) == 1

    def test_missing_replayed_user_event_is_uncertain_after_boundary(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session, request, procs = _build_session(tmp_path, monkeypatch, echo_user=False)
        session.initialize()
        with pytest.raises(bridge.SubmitUncertain):
            session.admit(_admission(request))
        assert len(procs[0].sent_messages) == 1

    def test_divergent_replayed_user_event_is_uncertain_after_boundary(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session, request, procs = _build_session(
            tmp_path, monkeypatch, echo_content="a different task text"
        )
        session.initialize()
        with pytest.raises(bridge.SubmitUncertain):
            session.admit(_admission(request))
        assert len(procs[0].sent_messages) == 1

    def test_uncertain_admission_never_retries_cleanly(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session, request, procs = _build_session(tmp_path, monkeypatch, emit_init=False)
        session.initialize()
        for _ in range(2):
            with pytest.raises(bridge.SubmitUncertain):
                session.admit(_admission(request))
        # Two sends crossed the boundary in this unit-level replay, and each
        # was refused as uncertain — never as a clean submission.  The
        # bridge-level delivery journal forbids the blind retry entirely.
        assert len(procs[0].sent_messages) == 2

    def test_marker_missing_after_session_start_is_refused_at_readiness(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session, request, procs = _build_session(tmp_path, monkeypatch, consume=False)
        with pytest.raises(bridge.BridgeError, match="wrapper-consumed marker"):
            session.initialize()


class TestAnthropicCompatibility:
    def test_anthropic_route_keeps_real_binary_and_no_marker(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session, request, procs = _build_session(
            tmp_path,
            monkeypatch,
            model="claude-3-7-sonnet-20250219",
            provider_route="anthropic",
        )
        readiness = session.initialize()
        assert procs[0].provider_argv[0] == request["provider_executable"]
        assert readiness["provider_receipt_kind"] == "claude-session-start"
        assert readiness["session_start"]["cwd"] == request["working_directory"]
        # Ordinary Anthropic ACP keeps its ambient credential path.
        assert procs[0].env.get("ANTHROPIC_API_KEY") == "sk-ant-ambient-fallback"
        # The first admission still captures and validates system/init for
        # the exact session/model/cwd on the Anthropic route.
        submission = session.admit(_admission(request))
        assert submission["provider_receipt_kind"] == "claude-turn-start"
        assert session._claude_init is not None
        assert session._claude_init["model"] == "claude-3-7-sonnet-20250219"

    def test_deepseek_model_on_anthropic_route_is_refused(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        request = _claude_request(tmp_path, model="deepseek-v4-flash", provider_route="anthropic")
        _deepseek_env_setup(monkeypatch, request)
        monkeypatch.setattr(bridge, "_profile_material", lambda *_: _material())
        monkeypatch.setattr(bridge._ProviderSession, "_version", lambda *_: "claude 2.1.233")
        session = bridge._ProviderSession(request)
        with pytest.raises(bridge.BridgeError, match="deepseek"):
            session.initialize()


class TestProfilePermissionPosture:
    def test_restricted_profile_passes_allowed_tools_and_no_skip(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session, request, procs = _build_session(
            tmp_path,
            monkeypatch,
            allowed_tools=["execute_bash", "fs_read"],
        )
        session.initialize()
        argv = procs[0].argv
        assert "--dangerously-skip-permissions" not in argv
        allowed_index = argv.index("--allowedTools")
        tools = set(argv[allowed_index + 1].split(","))
        # execute_bash maps to the Bash family; fs_read maps to Read.
        assert {"Bash", "BashOutput", "KillShell", "Read"} <= tools
        assert "WebFetch" not in tools

    def test_star_profile_keeps_explicit_skip_permissions(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session, request, procs = _build_session(tmp_path, monkeypatch, allowed_tools=["*"])
        session.initialize()
        argv = procs[0].argv
        assert "--dangerously-skip-permissions" in argv
        assert "--allowedTools" not in argv


def test_claude_acp_renderer_formats_stream_json_events() -> None:
    renderer = ManagedEventRenderer(provider="claude_code")

    assistant_text_event = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "Drafting code."}],
        },
    }
    assert renderer.render(assistant_text_event) == "Drafting code."

    tool_event = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}],
        },
    }
    assert "[tool] Bash — started" in (renderer.render(tool_event) or "")

    result_event = {
        "type": "result",
        "stop_reason": "end_turn",
    }
    assert "[turn completed] end_turn" in (renderer.render(result_event) or "")


def test_claude_acp_session_operation_route_query(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, request, procs = _build_session(tmp_path, monkeypatch)
    session.initialize()

    journal_path = tmp_path / "control-journal.json"
    journal = bridge.SessionControlJournal(journal_path)
    op_id = str(uuid.uuid4())
    op = journal.begin(
        operation_id=op_id,
        terminal_id=request["terminal_id"],
        generation=request["generation"],
        action="route-query",
        request_sha256="0" * 64,
        provider="claude_code",
        provider_session_id=session.provider_session_id or "session-id",
    )
    command = {
        "reservation_id": request["reservation_id"],
        "terminal_id": request["terminal_id"],
        "generation": request["generation"],
        "operation_id": op_id,
        "action": "route-query",
    }
    receipt = session.session_operation(command, journal)
    assert receipt["state"] == bridge.CONTROL_COMPLETED
    assert receipt["result"]["model"] == "deepseek-v4-flash"
    assert receipt["result"]["capabilities"]["follow_up"] is True
    assert receipt["result"]["capabilities"]["cancel"] is True
    assert receipt["result"]["capabilities"]["route_query"] is True


def test_claude_acp_session_operation_follow_up_and_cancel(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _claude_request(tmp_path)
    _deepseek_env_setup(monkeypatch, request)
    monkeypatch.setattr(bridge, "_profile_material", lambda *_: _material())
    procs: list[_FakeClaudeProcess] = []

    def fake_proc(*args: Any, **kwargs: Any) -> _FakeClaudeProcess:
        env = dict(kwargs.get("env") or {})
        env["_fake_init_event"] = {
            "type": "system",
            "subtype": "init",
            "session_id": "__from_argv__",
            "model": request["model"],
            "cwd": request["working_directory"],
        }
        env["_fake_consume_envelope"] = request["route_envelope"]
        kwargs["env"] = env
        proc = _FakeClaudeProcess(*args, **kwargs)
        proc.auto_complete = False  # keep the turn active until cancel
        procs.append(proc)
        return proc

    monkeypatch.setattr(bridge, "_RpcProcess", fake_proc)
    monkeypatch.setattr(bridge._ProviderSession, "_version", lambda *_: "claude 2.1.233")
    _session_start_hook(monkeypatch, request)

    session = bridge._ProviderSession(request)
    session.initialize()

    journal_path = tmp_path / "control-journal.json"
    journal = bridge.SessionControlJournal(journal_path)

    # Follow-up
    op_id = str(uuid.uuid4())
    op = journal.begin(
        operation_id=op_id,
        terminal_id=request["terminal_id"],
        generation=request["generation"],
        action="follow-up",
        request_sha256="1" * 64,
        provider="claude_code",
        provider_session_id=session.provider_session_id or "session-id",
    )
    command = {
        "reservation_id": request["reservation_id"],
        "terminal_id": request["terminal_id"],
        "generation": request["generation"],
        "operation_id": op_id,
        "action": "follow-up",
        "message": "check next step",
    }
    receipt = session.session_operation(command, journal)
    assert receipt["state"] == bridge.CONTROL_ACCEPTED
    assert receipt["provider_turn_id"] is not None

    # Refuse second concurrent follow-up while the turn is active
    second_op_id = str(uuid.uuid4())
    second_op = journal.begin(
        operation_id=second_op_id,
        terminal_id=request["terminal_id"],
        generation=request["generation"],
        action="follow-up",
        request_sha256="3" * 64,
        provider="claude_code",
        provider_session_id=session.provider_session_id or "session-id",
    )
    second_command = {
        "reservation_id": request["reservation_id"],
        "terminal_id": request["terminal_id"],
        "generation": request["generation"],
        "operation_id": second_op_id,
        "action": "follow-up",
        "message": "concurrent prompt",
    }
    second_receipt = session.session_operation(second_command, journal)
    assert second_receipt["state"] == bridge.CONTROL_REFUSED
    assert second_receipt["reason_code"] == "turn_busy"

    # Cancel while the turn is active
    cancel_op_id = str(uuid.uuid4())
    cancel_op = journal.begin(
        operation_id=cancel_op_id,
        terminal_id=request["terminal_id"],
        generation=request["generation"],
        action="cancel",
        request_sha256="2" * 64,
        provider="claude_code",
        provider_session_id=session.provider_session_id or "session-id",
    )
    cancel_command = {
        "reservation_id": request["reservation_id"],
        "terminal_id": request["terminal_id"],
        "generation": request["generation"],
        "operation_id": cancel_op_id,
        "action": "cancel",
    }
    cancel_receipt = session.session_operation(cancel_command, journal)
    assert cancel_receipt["state"] == bridge.CONTROL_ACCEPTED

    # Reconcile after cancel completes (clear the active prompt lock)
    session._active_prompt_request_id = None
    reconciled = session.reconcile_session_operation(journal, cancel_op_id)
    assert reconciled["state"] == bridge.CONTROL_COMPLETED


def test_claude_code_is_in_authoritative_readiness_and_submission_maps() -> None:
    # Verify exact readiness receipt and submission receipt kinds
    assert managed_launch._READINESS_RECEIPT_KINDS["claude_code"] == "claude-session-start"
    assert managed_launch._SUBMISSION_RECEIPT_KINDS["claude_code"] == "claude-turn-start"
    assert "claude_code" in managed_launch.READINESS_PROVIDERS
