"""Claude provider hook receiver (SessionStart/PostToolUse/Stop).

For Claude, the heartbeat producer is the provider adapter hook
receiver: the provider's hooks POST to a generation-private local
receiver, and each accepted hook is provider-native beat evidence
(``hook_event``) carrying the payload ``session_id``, the hook event
name, and the tool sequence.  The trusted ``SessionStart`` hook is also
the pre-turn identity receipt for the explicit ``--session-id <uuid>``
launch form; the transcript path it attests is corroboration only.

Invariant: a hook is accepted only for the exact bound native session
id of this generation; tool sequences are monotone per session; a
fenced (sealed) generation refuses further activity hooks while still
recording terminal quiescence hooks; hook transport is a
generation-private Unix-domain socket, loopback/owner-only.

Failure mode prevented: accepting hooks by payload alone would let any
local process forge provider activity for a generation (false life) or
inject terminal evidence (false death); binding every hook to the
generation-private socket and the exact session id is what makes the
receipt provider-native.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from cli_agent_orchestrator.services import generation_fence, heartbeat_store

HOOK_EVENTS = ("SessionStart", "PostToolUse", "Stop", "SessionEnd")
TERMINAL_HOOK_EVENTS = ("Stop", "SessionEnd")

HOOK_RECEIPT_SCHEMA = "cao-claude-hook-receipt-v1"


class ClaudeHookError(RuntimeError):
    """Base error for Claude hook handling."""


class ClaudeHookRefused(ClaudeHookError):
    """A hook failed session binding, sequence, or fence checks."""


class ClaudeHookReceiver:
    """Generation-private receiver binding hooks to one exact session."""

    def __init__(
        self,
        *,
        companion_dir: Path,
        producer: heartbeat_store.HeartbeatProducer,
        native_session_id: str,
        terminal_id: str,
        generation: str,
    ) -> None:
        self._dir = Path(companion_dir)
        self._producer = producer
        self._session = native_session_id
        self._terminal = terminal_id
        self._generation = generation
        self._last_tool_sequence = 0
        self._session_started = False

    def handle_hook(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Validate one hook payload and emit the corresponding beat.

        Returns the hook receipt.  Refusals raise ``ClaudeHookRefused``
        (or ``generation_fence.FencedError`` for post-seal activity);
        neither advances any state.
        """
        event = payload.get("hook_event_name")
        if event not in HOOK_EVENTS:
            raise ClaudeHookRefused(f"unknown hook event: {event!r}")
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ClaudeHookRefused("hook payload lacks a session_id")
        if session_id != self._session:
            raise ClaudeHookRefused(
                "hook session_id does not match this generation's bound native "
                "session; hooks from any other session are not evidence here"
            )

        if event == "SessionStart":
            turn_state = "active"
            evidence_id = f"hook:SessionStart:{session_id}"
            self._session_started = True
        elif event == "PostToolUse":
            # A sealed generation prevents post-report tool admission;
            # the hook reporting one is refused like any fenced input.
            generation_fence.assert_admission_open(self._dir, self._terminal, self._generation)
            sequence = payload.get("tool_sequence")
            if not isinstance(sequence, int) or sequence <= self._last_tool_sequence:
                raise ClaudeHookRefused(
                    f"tool_sequence must strictly increase (last "
                    f"{self._last_tool_sequence}, got {sequence!r})"
                )
            self._last_tool_sequence = sequence
            turn_state = "active"
            evidence_id = f"hook:PostToolUse:{session_id}:{sequence}"
        else:
            turn_state = "terminal"
            evidence_id = f"hook:{event}:{session_id}"

        provider_turn_id = payload.get("turn_id") or session_id
        if turn_state == "terminal":
            record = self._producer.terminal_beat(
                provider_turn_id=str(provider_turn_id),
                evidence_kind="hook_event",
                evidence_id=evidence_id,
            )
        else:
            record = self._producer.beat(
                turn_state="active",
                provider_turn_id=str(provider_turn_id),
                evidence_kind="hook_event",
                evidence_id=evidence_id,
                force=event == "SessionStart",
            )
        return {
            "schema": HOOK_RECEIPT_SCHEMA,
            "hook_event_name": event,
            "session_id": session_id,
            "terminal_id": self._terminal,
            "generation": self._generation,
            "accepted": True,
            "beat_written": record is not None,
        }

    @property
    def session_started(self) -> bool:
        return self._session_started

    @property
    def last_tool_sequence(self) -> int:
        return self._last_tool_sequence


def socket_path(companion_dir: Path, terminal_id: str, generation: str) -> Path:
    """The generation-private hook receiver socket path (0600 at bind)."""
    return Path(companion_dir) / terminal_id / generation / "hooks.sock"


def parse_hook_body(raw: bytes) -> dict[str, Any]:
    """Parse one hook POST body; malformed bodies are refused."""
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClaudeHookRefused("hook body is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ClaudeHookRefused("hook body is not a JSON object")
    return payload
