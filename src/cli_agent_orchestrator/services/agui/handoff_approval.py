"""Human-in-the-loop approval stack for agent handoff and permission prompts.

Components:
- ``classify_reason``: total, deterministic classifier that maps provider + raw
  prompt text to a structured ``namespace:local_name`` reason string.
- ``ApprovalDecision``: enum of possible user decisions.
- ``Interrupt``: frozen record of a pending (or resolved) approval request.
- ``AgentHandoffWithApproval``: L2 construct managing the full interrupt lifecycle.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Protocol

from cli_agent_orchestrator.services.agui.base import AguiConstruct, RecordingUiEmitter, UiEmitter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Provider pattern imports (re-use existing detection patterns)
# ---------------------------------------------------------------------------

from cli_agent_orchestrator.providers.claude_code import (
    TRUST_PROMPT_PATTERN as CLAUDE_TRUST_PATTERN,
)
from cli_agent_orchestrator.providers.claude_code import (
    WAITING_USER_ANSWER_PATTERN as CLAUDE_WAITING_PATTERN,
)
from cli_agent_orchestrator.providers.codex import TRUST_PROMPT_PATTERN as CODEX_TRUST_PATTERN
from cli_agent_orchestrator.providers.codex import WAITING_PROMPT_PATTERN as CODEX_WAITING_PATTERN
from cli_agent_orchestrator.providers.kiro_cli import TUI_PERMISSION_PATTERN as KIRO_TUI_PATTERN

# Legacy kiro permission pattern (instantiated on the provider instance normally,
# but we duplicate the static regex here for the classifier).
KIRO_LEGACY_PERMISSION_PATTERN = r"Allow this action\?.*?\[.*?y.*?/.*?n.*?/.*?t.*?\]:"


# ---------------------------------------------------------------------------
# classify_reason: total, deterministic, NEVER raises
# ---------------------------------------------------------------------------

# Namespace map: provider name -> namespace segment.
_NAMESPACE_MAP: Dict[str, str] = {
    "kiro_cli": "kiro",
    "claude_code": "claude-code",
    "codex": "codex",
}


def _to_kebab(name: str) -> str:
    """Convert a provider name to kebab-case namespace (lowercase, hyphens)."""
    # Replace underscores with hyphens, strip non-alphanumeric/hyphen chars.
    result = re.sub(r"[^a-z0-9-]", "-", name.lower().replace("_", "-"))
    # Collapse multiple hyphens.
    result = re.sub(r"-+", "-", result).strip("-")
    return result or "unknown"


def classify_reason(provider: str, raw_prompt: str) -> str:
    """Classify a provider waiting prompt into a structured reason string.

    Returns ``namespace:local_name`` where:
    - namespace matches ``^[a-z0-9-]+$``
    - local_name matches ``^[a-z0-9_]+$``
    - NEVER returns ``core:*`` (reserved by ag-ui)

    This function is total and deterministic: it never raises for any input.
    """
    try:
        # Determine namespace
        namespace = _NAMESPACE_MAP.get(provider, _to_kebab(provider))
        # Safety: never produce "core" namespace
        if namespace == "core":
            namespace = "provider-core"

        # Per-provider classification
        local_name = _classify_local(provider, raw_prompt)
        return f"{namespace}:{local_name}"
    except Exception:  # pragma: no cover - total-function safety net
        # Total: absorb any unexpected error
        namespace = _NAMESPACE_MAP.get(provider, _to_kebab(provider)) if provider else "unknown"
        if namespace == "core":
            namespace = "provider-core"
        return f"{namespace}:unknown_prompt"


def _classify_local(provider: str, raw_prompt: str) -> str:
    """Determine the local_name for a given provider and prompt text."""
    if provider == "claude_code":
        # Trust prompt takes priority (it also matches WAITING pattern sometimes)
        if re.search(CLAUDE_TRUST_PATTERN, raw_prompt):
            return "trust_prompt"
        if re.search(CLAUDE_WAITING_PATTERN, raw_prompt):
            return "permission_request"
        return "unknown_prompt"

    elif provider == "kiro_cli":
        # TUI permission pattern (check specific patterns before the generic "trust" word)
        if re.search(KIRO_TUI_PATTERN, raw_prompt):
            return "permission_request"
        # Legacy permission pattern
        if re.search(KIRO_LEGACY_PERMISSION_PATTERN, raw_prompt):
            return "permission_request"
        # Trust-related wording (generic, checked last)
        if re.search(r"trust", raw_prompt, re.IGNORECASE):
            return "trust_prompt"
        return "unknown_prompt"

    elif provider == "codex":
        # Trust prompt
        if re.search(CODEX_TRUST_PATTERN, raw_prompt):
            return "trust_prompt"
        # Approval request
        if re.search(CODEX_WAITING_PATTERN, raw_prompt, re.MULTILINE):
            return "approval_request"
        return "unknown_prompt"

    else:
        return "unknown_prompt"


# ---------------------------------------------------------------------------
# ApprovalDecision enum
# ---------------------------------------------------------------------------


class ApprovalDecision(str, Enum):
    """Possible decisions a user can make on an approval interrupt."""

    APPROVE = "approve"
    DENY = "deny"
    EDIT = "edit"


# ---------------------------------------------------------------------------
# Interrupt dataclass
# ---------------------------------------------------------------------------


@dataclass
class Interrupt:
    """Record of a pending or resolved approval request."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    reason: str = ""
    message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    options: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    expires_at: Optional[str] = None
    resolved: bool = False
    outcome: Optional[str] = None


# ---------------------------------------------------------------------------
# AnswerDelivery protocol (for dependency injection in tests)
# ---------------------------------------------------------------------------


class AnswerDelivery(Protocol):
    """Protocol for delivering an answer to a terminal."""

    def send_input(
        self, terminal_id: str, text: str, **kwargs: Any
    ) -> None: ...  # pragma: no cover

    def send_special_key(self, terminal_id: str, key: str) -> bool: ...  # pragma: no cover


class TerminalServiceAnswerDelivery:
    """Production ``AnswerDelivery`` backed by ``terminal_service``.

    Delivers a resolved approval decision to the waiting CLI by driving the same
    tmux input path the rest of CAO uses. ``terminal_service`` is imported lazily
    (mirroring ``approval_bridge``) so the metadata-only ``services/agui`` package
    keeps its import graph free of the heavy terminal layer at module load.

    Must only be invoked from the CAO server event loop (the construct's
    ``resume`` critical section calls it); ``terminal_service.send_input`` is
    blocking tmux I/O, so a single delivery briefly runs on the loop thread.
    """

    def send_input(self, terminal_id: str, text: str, **kwargs: Any) -> None:
        from cli_agent_orchestrator.services import terminal_service

        terminal_service.send_input(terminal_id, text)

    def send_special_key(self, terminal_id: str, key: str) -> bool:
        from cli_agent_orchestrator.services import terminal_service

        return terminal_service.send_special_key(terminal_id, key)


# ---------------------------------------------------------------------------
# Per-provider answer translation
# ---------------------------------------------------------------------------

# ANSI/VT escape sequences: CSI (ESC[ ... final), OSC (ESC] ... BEL/ST), and
# lone two-char escapes (ESC + Fe). C0/C1 control bytes (incl. NUL) are stripped
# separately, preserving only tab/newline/carriage-return.
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC ... BEL or ST
    r"|\x1b[@-Z\\-_]"  # two-char escapes
)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _sanitize_edited_text(text: str) -> str:
    """Strip ANSI/VT escape sequences and control bytes (incl. NUL) from
    operator-supplied edit text before it is written to a terminal via
    ``send_input``.

    Preserves tab / newline / carriage-return. Prevents terminal
    escape-sequence injection through the approval edit path (P1-4). Any bare
    ESC left after escape-sequence removal is caught by the control-byte pass
    (0x1b is within \\x0e-\\x1f).
    """
    without_ansi = _ANSI_ESCAPE_RE.sub("", text)
    return _CONTROL_CHARS_RE.sub("", without_ansi)


def _translate_decision(
    provider: str,
    decision: ApprovalDecision,
    edited_text: Optional[str] = None,
) -> Dict[str, Any]:
    """Translate a user decision into provider-specific terminal input.

    Returns a dict with either:
    - {"type": "text", "value": str} for text input
    - {"type": "key", "value": str} for special key
    """
    if decision == ApprovalDecision.EDIT:
        # Edit always sends the edited text, sanitized against terminal escape
        # injection (ANSI/VT sequences + control bytes) before send_input.
        return {"type": "text", "value": _sanitize_edited_text(edited_text or "")}

    if provider == "claude_code":
        if decision == ApprovalDecision.APPROVE:
            return {"type": "key", "value": "Enter"}
        else:  # deny
            return {"type": "key", "value": "Escape"}

    elif provider == "kiro_cli":
        if decision == ApprovalDecision.APPROVE:
            return {"type": "text", "value": "y"}
        else:  # deny
            return {"type": "text", "value": "n"}

    elif provider == "codex":
        if decision == ApprovalDecision.APPROVE:
            return {"type": "text", "value": "y"}
        else:  # deny
            return {"type": "text", "value": "n"}

    else:
        # Generic fallback
        if decision == ApprovalDecision.APPROVE:
            return {"type": "text", "value": "y"}
        else:
            return {"type": "text", "value": "n"}


# Default options per reason category
_DEFAULT_OPTIONS: Dict[str, List[str]] = {
    "permission_request": ["approve", "deny", "edit"],
    "trust_prompt": ["approve", "deny"],
    "approval_request": ["approve", "deny", "edit"],
    "unknown_prompt": ["approve", "deny"],
}


def _options_for_reason(reason: str) -> List[str]:
    """Determine available options based on the classified reason."""
    # Extract local_name from "namespace:local_name"
    parts = reason.split(":", 1)
    local_name = parts[1] if len(parts) == 2 else reason
    return _DEFAULT_OPTIONS.get(local_name, ["approve", "deny"])


# ---------------------------------------------------------------------------
# Registry bounds
# ---------------------------------------------------------------------------

_REGISTRY_CAP = 1000
_RESOLVED_TTL_SECONDS = 300.0


# ---------------------------------------------------------------------------
# AgentHandoffWithApproval construct
# ---------------------------------------------------------------------------


class AgentHandoffWithApproval(AguiConstruct):
    """L2 construct managing the full human-in-the-loop approval lifecycle.

    Features:
    - Creates Interrupt records when a provider enters WAITING_USER_ANSWER.
    - Resolves interrupts exactly once (lock-guarded).
    - Translates decisions to per-provider terminal input.
    - Expires interrupts with zero keystrokes on status transitions.
    - Bounded registry with TTL eviction for resolved entries.

    Concurrency invariant (single event loop):
        ``resume()`` guards mutations with an ``asyncio.Lock``, but the sync
        methods ``on_provider_waiting()`` and ``expire()`` mutate the same
        shared registries (``_interrupts``, ``_terminal_to_interrupt``,
        ``_resolved_at``) WITHOUT that lock. This is safe ONLY when every
        caller runs on the one asyncio event loop (the CAO server model): a
        sync method and the async ``resume`` critical section can never truly
        interleave because control yields only at ``await`` points. Do NOT call
        these methods from a separate OS thread or a second event loop — doing
        so reintroduces the check-then-act races the lock exists to prevent.
    """

    def __init__(
        self,
        emitter: UiEmitter,
        answer_delivery: Optional[AnswerDelivery] = None,
    ) -> None:
        super().__init__(emitter)
        self._answer_delivery = answer_delivery
        self._interrupts: Dict[str, Interrupt] = {}
        # Map terminal_id -> interrupt_id for quick lookup of open interrupts
        self._terminal_to_interrupt: Dict[str, str] = {}
        self._lock = asyncio.Lock()
        # Track resolution timestamps for TTL eviction
        self._resolved_at: Dict[str, float] = {}

    def handle_frame(
        self, agui_type: str, data: Dict[str, Any], event_id: Optional[str] = None
    ) -> None:
        """Not used for event-driven processing; this construct is API-driven."""
        pass

    def projection(self) -> Dict[str, Any]:
        """Return current state as JSON-serializable dict."""
        return {
            "pending": [_interrupt_to_dict(i) for i in self._interrupts.values() if not i.resolved],
            "total": len(self._interrupts),
        }

    def on_provider_waiting(
        self,
        terminal_id: str,
        provider: str,
        raw_prompt: str,
        session_name: Optional[str] = None,
    ) -> Interrupt:
        """Create an Interrupt when a provider enters WAITING_USER_ANSWER.

        Classifies the reason, builds the interrupt record, emits an
        approval_card UI intent, and returns the interrupt.
        """
        reason = classify_reason(provider, raw_prompt)
        # Redact message to <= 256 chars
        message = raw_prompt[:256] if raw_prompt else ""

        options = _options_for_reason(reason)

        interrupt = Interrupt(
            reason=reason,
            message=message,
            metadata={
                "provider": provider,
                "terminal_id": terminal_id,
                "session_name": session_name,
                "source_event_id": None,
            },
            options=options,
        )

        self._interrupts[interrupt.id] = interrupt
        self._terminal_to_interrupt[terminal_id] = interrupt.id

        # Evict if over cap
        self._evict_if_needed()

        # Emit approval_card UI intent
        try:
            self.emit(
                "approval_card",
                {
                    "interrupt_id": interrupt.id,
                    "reason": reason,
                    "message": message,
                    "options": options,
                    "provider": provider,
                    "terminal_id": terminal_id,
                },
                terminal_id=terminal_id,
                session_name=session_name,
            )
        except (ValueError, RuntimeError):
            # Emit failure should not block interrupt creation
            logger.debug("Failed to emit approval_card for interrupt %s", interrupt.id)

        return interrupt

    async def resume(
        self,
        interrupt_id: str,
        decision: ApprovalDecision,
        edited_text: Optional[str] = None,
    ) -> Interrupt:
        """Resolve an interrupt with the user's decision.

        Lock-guarded for exactly-once resolution. Returns the interrupt
        (with outcome set). If already resolved, returns the recorded outcome
        with no side effects (idempotent).

        Raises:
            KeyError: if interrupt_id is unknown
            ValueError: if decision is invalid for this interrupt
        """
        async with self._lock:
            interrupt = self._interrupts.get(interrupt_id)
            if interrupt is None:
                raise KeyError(f"Unknown interrupt: {interrupt_id}")

            # Idempotent: already resolved -> return recorded outcome
            if interrupt.resolved:
                return interrupt

            # Validate decision is in supported options
            if decision.value not in interrupt.options:
                raise ValueError(
                    f"Decision '{decision.value}' not supported for this interrupt. "
                    f"Allowed: {interrupt.options}"
                )

            # Validate edit text
            if decision == ApprovalDecision.EDIT:
                if not edited_text or not edited_text.strip():
                    raise ValueError("Edit decision requires non-empty edited_text")
                if len(edited_text) > 4000:
                    raise ValueError(f"edited_text too long ({len(edited_text)} chars, max 4000)")

            # Resolve the interrupt
            interrupt.resolved = True
            interrupt.outcome = decision.value
            self._resolved_at[interrupt_id] = time.monotonic()

            # Remove from terminal map
            terminal_id = interrupt.metadata.get("terminal_id")
            if terminal_id and self._terminal_to_interrupt.get(terminal_id) == interrupt_id:
                del self._terminal_to_interrupt[terminal_id]

            # Deliver the answer to the terminal
            provider = interrupt.metadata.get("provider", "")
            action = _translate_decision(provider, decision, edited_text)

            if self._answer_delivery and terminal_id:
                try:
                    if action["type"] == "text":
                        self._answer_delivery.send_input(terminal_id, action["value"])
                    elif action["type"] == "key":
                        self._answer_delivery.send_special_key(terminal_id, action["value"])
                except Exception as e:
                    logger.warning(
                        "Failed to deliver answer for interrupt %s: %s",
                        interrupt_id,
                        e,
                    )

            # Emit resolution intent
            try:
                self.emit(
                    "approval_card",
                    {
                        "interrupt_id": interrupt_id,
                        "resolved": True,
                        "outcome": decision.value,
                        "provider": provider,
                        "terminal_id": terminal_id,
                    },
                    terminal_id=terminal_id,
                    session_name=interrupt.metadata.get("session_name"),
                )
            except (ValueError, RuntimeError):
                logger.debug("Failed to emit resolution for interrupt %s", interrupt_id)

            return interrupt

    def expire(self, terminal_id: str) -> Optional[Interrupt]:
        """Expire the open interrupt for a terminal (zero keystrokes).

        Returns the expired interrupt, or None if no open interrupt exists.
        """
        interrupt_id = self._terminal_to_interrupt.get(terminal_id)
        if interrupt_id is None:
            return None

        interrupt = self._interrupts.get(interrupt_id)
        if interrupt is None or interrupt.resolved:
            # Clean up stale mapping
            self._terminal_to_interrupt.pop(terminal_id, None)
            return None

        # Resolve as expired with ZERO keystrokes
        interrupt.resolved = True
        interrupt.outcome = "expired"
        self._resolved_at[interrupt_id] = time.monotonic()

        # Remove from terminal map
        del self._terminal_to_interrupt[terminal_id]

        # Emit expiration intent
        try:
            self.emit(
                "approval_card",
                {
                    "interrupt_id": interrupt_id,
                    "resolved": True,
                    "outcome": "expired",
                    "provider": interrupt.metadata.get("provider", ""),
                    "terminal_id": terminal_id,
                },
                terminal_id=terminal_id,
                session_name=interrupt.metadata.get("session_name"),
            )
        except (ValueError, RuntimeError):
            logger.debug("Failed to emit expiration for interrupt %s", interrupt_id)

        return interrupt

    def pending(self) -> List[Interrupt]:
        """Return all unresolved interrupts."""
        return [i for i in self._interrupts.values() if not i.resolved]

    def get_interrupt(self, interrupt_id: str) -> Optional[Interrupt]:
        """Return an interrupt by ID, or None if not found."""
        return self._interrupts.get(interrupt_id)

    def _evict_if_needed(self) -> None:
        """Evict resolved entries beyond the TTL, then oldest resolved if over cap."""
        now = time.monotonic()

        # First pass: evict resolved entries past TTL
        expired_ids = [
            iid
            for iid, resolved_time in self._resolved_at.items()
            if now - resolved_time >= _RESOLVED_TTL_SECONDS
        ]
        for iid in expired_ids:
            self._interrupts.pop(iid, None)
            self._resolved_at.pop(iid, None)

        # Second pass: if still over cap, evict oldest resolved first
        while len(self._interrupts) > _REGISTRY_CAP:
            # Find oldest resolved
            oldest_id = None
            oldest_time = float("inf")
            for iid, resolved_time in self._resolved_at.items():
                if resolved_time < oldest_time:
                    oldest_time = resolved_time
                    oldest_id = iid
            if oldest_id:
                self._interrupts.pop(oldest_id, None)
                self._resolved_at.pop(oldest_id, None)
            else:
                break  # No resolved entries to evict


def _interrupt_to_dict(interrupt: Interrupt) -> Dict[str, Any]:
    """Convert an Interrupt to a JSON-serializable dict."""
    return {
        "id": interrupt.id,
        "reason": interrupt.reason,
        "message": interrupt.message,
        "metadata": interrupt.metadata,
        "options": interrupt.options,
        "created_at": interrupt.created_at,
        "expires_at": interrupt.expires_at,
        "resolved": interrupt.resolved,
        "outcome": interrupt.outcome,
    }


__all__ = [
    "AgentHandoffWithApproval",
    "AnswerDelivery",
    "ApprovalDecision",
    "Interrupt",
    "classify_reason",
]
