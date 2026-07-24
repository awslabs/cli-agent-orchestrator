"""Human-readable projection of provider-native managed-session events.

The structured RPC stream remains the source of truth.  This module only
projects that stream into a bounded terminal view; it never feeds rendered
text back into lifecycle decisions.  Unknown payloads are summarized by
their method and event kind so a provider upgrade cannot dump raw protocol
records, prompt bodies, or tool results into the operator pane.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


def _text(value: Any) -> str:
    if isinstance(value, dict):
        candidate = value.get("text")
        return candidate if isinstance(candidate, str) else ""
    if isinstance(value, list):
        return "".join(_text(item.get("content", item)) for item in value if isinstance(item, dict))
    return ""


def _label(value: Any, fallback: str) -> str:
    if isinstance(value, str) and value.strip():
        return " ".join(value.strip().split())[:160]
    return fallback


@dataclass
class ManagedEventRenderer:
    """Stateful, low-noise renderer for ACP and Codex app-server events."""

    provider: str
    _tool_states: dict[str, tuple[str, str]] = field(default_factory=dict)

    def render(self, item: dict[str, Any]) -> Optional[str]:
        method = item.get("method")
        if method == "session/update":
            return self._render_acp_update(item.get("params"))
        if isinstance(method, str):
            return self._render_codex_or_unknown(method, item.get("params"))
        return "[provider event]\n"

    def _render_acp_update(self, params: Any) -> Optional[str]:
        update = params.get("update") if isinstance(params, dict) else None
        if not isinstance(update, dict):
            return "[provider event] session update\n"
        kind = update.get("sessionUpdate")
        if kind in {"agent_message_chunk", "agent_thought_chunk", "user_message_chunk"}:
            content = _text(update.get("content"))
            return content if content else None
        if kind in {"tool_call", "tool_call_update"}:
            tool_id = str(update.get("toolCallId") or "tool")
            title = _label(update.get("title") or update.get("kind"), "tool")
            status = _label(update.get("status"), "started")
            current = (title, status)
            if self._tool_states.get(tool_id) == current:
                return None
            self._tool_states[tool_id] = current
            return f"\n[tool] {title} — {status}\n"
        if kind in {"plan", "plan_update"}:
            entries = update.get("entries")
            if not isinstance(entries, list):
                return "[plan updated]\n"
            lines = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                status = _label(entry.get("status"), "pending")
                title = _label(entry.get("title") or entry.get("content"), "step")
                lines.append(f"  [{status}] {title}")
            return "[plan]\n" + "\n".join(lines) + "\n" if lines else "[plan updated]\n"
        if kind == "plan_removed":
            return "[plan cleared]\n"
        if kind == "available_commands_update":
            commands = update.get("availableCommands")
            names = (
                [
                    _label(command.get("name"), "")
                    for command in commands
                    if isinstance(command, dict) and command.get("name")
                ]
                if isinstance(commands, list)
                else []
            )
            return f"[commands] {', '.join(names)}\n" if names else "[commands] none advertised\n"
        if kind == "config_option_update":
            config_id = _label(update.get("configId"), "configuration")
            value = _label(update.get("value") or update.get("currentValue"), "updated")
            return f"[route] {config_id}={value}\n"
        if kind == "current_mode_update":
            return f"[mode] {_label(update.get('currentModeId'), 'updated')}\n"
        if kind == "session_info_update":
            return "[session metadata updated]\n"
        if kind == "usage_update":
            return "[usage updated]\n"
        return f"[provider event] {_label(kind, 'session update')}\n"

    def _render_codex_or_unknown(self, method: str, params: Any) -> Optional[str]:
        # Codex app-server delta names have changed between pinned releases.
        # Match semantic suffixes and only project display text; receipt logic
        # continues to consume the untouched structured notification.
        if method.endswith("/delta") and isinstance(params, dict):
            delta = params.get("delta")
            if isinstance(delta, str):
                return delta
            text = _text(delta)
            return text or None
        if method in {"turn/started", "turn/completed", "turn/failed", "turn/cancelled"}:
            return f"\n[{method.replace('/', ' ')}]\n"
        if "tool" in method or "item" in method:
            return f"[provider event] {_label(method, 'tool update')}\n"
        return f"[provider event] {_label(method, 'update')}\n"
