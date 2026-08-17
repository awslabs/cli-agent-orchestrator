"""Static provider-terminal recovery detection and durable episode identity.

M6a is deliberately an observation contract.  It recognizes a tiny locally
proven Claude allowlist and publishes enough exact identity for later layers to
decide what to do.  Nothing in this module sends input, wakes a supervisor,
claims completion, or changes a task.

The detector dispatches by the stored provider name rather than a live provider
object.  A daemon restarted after a provider was launched can therefore read a
settled pane and recover the same durable occurrence from SQLite.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Sequence, cast

from sqlalchemy.exc import IntegrityError, OperationalError

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.terminal import TerminalStatus

logger = logging.getLogger(__name__)

SCHEMA = "cao.provider-recovery-evidence.v1"
DETECTOR = "cao-provider-terminal-error"
DETECTOR_VERSION = "1"
RAW_TEXT_MAX_BYTES = 1024
PRE_COMPOSER_LOGICAL_LINE_MAX_ROWS = 3
SQLITE_CONTENTION_RETRY_DELAYS = (0.01, 0.05)

TURN_TERMINAL = "terminal"
TURN_SELF_RETRYING = "self-retrying"
TURN_UNKNOWN = "unknown"
TURN_STATES = frozenset({TURN_TERMINAL, TURN_SELF_RETRYING, TURN_UNKNOWN})

ACTION_NUDGE = "nudge"
ACTION_IGNORE = "ignore"
ACTION_LAYER_2 = "layer-2"
RECOVERY_ACTIONS = frozenset({ACTION_NUDGE, ACTION_IGNORE, ACTION_LAYER_2})

_GLYPH = r"(?:[⏺⚠■ⓘ⎿✻•└]\s*)?"
_CONNECTION_CLOSED = re.compile(
    rf"^\s*{_GLYPH}API Error: Connection closed mid-response\. "
    r"The response above may be incomplete\.\s*$"
)
# Claude Code 2.1.233 reworded the same line to "Connection lost".  The
# 2.1.224 wording is absent from that build's bundle, so matching only it
# leaves the detector blind on any current install; both are kept because a
# worker may be running either build.  Distinct pattern ids keep the durable
# occurrence fingerprint stable and record which build's text was seen.
_CONNECTION_LOST = re.compile(
    rf"^\s*{_GLYPH}API Error: Connection lost mid-response\. "
    r"The response above may be incomplete\.\s*$"
)
_MID_RESPONSE_TERMINALS = (
    # (expression, pattern, provenance) -- the two wordings do NOT share
    # provenance.  The closed wording was captured on a live pane here; the lost
    # wording is read out of the 2.1.233 bundle and has never been seen render.
    # The journal must not report the second as observed.
    (_CONNECTION_CLOSED, "claude.connection-closed-mid-response", "locally observed"),
    (_CONNECTION_LOST, "claude.connection-lost-mid-response", "read from the shipped bundle"),
)
_RETRY_BANNER = re.compile(
    rf"^\s*{_GLYPH}API error\s*[·-]\s*Retrying in\s+\S+\s*[·-]\s*" r"attempt\s+\d+/\d+\s*$",
    re.IGNORECASE,
)
_GENERIC_API_ERROR = re.compile(rf"^\s*{_GLYPH}API Error:\s+\S.*$")
_RAIL = re.compile(r"─{8,}")
_PROMPT = re.compile(r"[>❯](?:[\s\xa0]|$)")


class RecoveryEvidenceUnavailable(RuntimeError):
    """The durable occurrence store could not reconcile an observation."""


@dataclass(frozen=True)
class RecoveryMatch:
    pattern: str
    turn_state: str
    recovery_action: str
    status: Optional[TerminalStatus]
    confidence: str
    reason: str
    raw_text: str
    raw_sha256: str
    raw_text_truncated: bool
    signals: list[dict[str, Any]]

    @property
    def fingerprint(self) -> str:
        value = (
            f"{SCHEMA}\0{DETECTOR_VERSION}\0{self.pattern}\0"
            f"{self.turn_state}\0{self.recovery_action}"
        )
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def stored_payload(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "detector": DETECTOR,
            "detector_version": DETECTOR_VERSION,
            "pattern": self.pattern,
            "turn_state": self.turn_state,
            "recovery_action": self.recovery_action,
            "confidence": self.confidence,
            "reason": self.reason,
            "signals": self.signals,
            "raw_text": self.raw_text,
            "raw_sha256": self.raw_sha256,
            "raw_text_truncated": self.raw_text_truncated,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _bounded_text(value: str) -> tuple[str, bool]:
    raw = value.encode("utf-8")
    if len(raw) <= RAW_TEXT_MAX_BYTES:
        return value, False
    bounded = raw[:RAW_TEXT_MAX_BYTES]
    while bounded:
        try:
            return bounded.decode("utf-8"), True
        except UnicodeDecodeError as exc:
            bounded = bounded[: exc.start]
    return "", True


def _match(
    *,
    pattern: str,
    turn_state: str,
    recovery_action: str,
    status: Optional[TerminalStatus],
    confidence: str,
    reason: str,
    raw_text: str,
    idle_composer: Optional[bool] = None,
) -> RecoveryMatch:
    bounded, truncated = _bounded_text(raw_text)
    signals: list[dict[str, Any]] = [
        {
            "name": "provider-error-pattern",
            "state": "available",
            "value": pattern,
            "detail": f"matched by {DETECTOR}@{DETECTOR_VERSION}",
        }
    ]
    if idle_composer is not None:
        signals.append(
            {
                "name": "idle-composer",
                "state": "available",
                "value": idle_composer,
                "detail": "Claude boxed composer is visible on the settled viewport",
            }
        )
    return RecoveryMatch(
        pattern=pattern,
        turn_state=turn_state,
        recovery_action=recovery_action,
        status=status,
        confidence=confidence,
        reason=reason,
        raw_text=bounded,
        raw_sha256=hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        raw_text_truncated=truncated,
        signals=signals,
    )


def _boxed_composer_top(rows: Sequence[str]) -> Optional[int]:
    """Return the upper rail of the newest complete Claude composer."""
    rails = [index for index, line in enumerate(rows) if _RAIL.search(line)]
    prompts = [index for index, line in enumerate(rows) if _PROMPT.search(line)]
    for prompt in reversed(prompts):
        upper = [rail for rail in rails if 0 < prompt - rail <= 2]
        if upper and any(0 < rail - prompt <= 2 for rail in rails):
            return max(upper)
    return None


def _tail_candidates(rows: Sequence[str]) -> list[str]:
    """Bounded logical-line suffixes ending immediately before a composer."""
    width = min(len(rows), PRE_COMPOSER_LOGICAL_LINE_MAX_ROWS)
    return [" ".join(str(row).strip() for row in rows[-size:]) for size in range(width, 0, -1)]


def detect(provider: str, screen_lines: Optional[Sequence[str]]) -> Optional[RecoveryMatch]:
    """Classify one settled viewport using the static, provider-name allowlist."""
    if provider != "claude_code" or screen_lines is None:
        return None

    rows = [str(line).rstrip() for line in screen_lines if str(line).strip()]
    composer_top = _boxed_composer_top(rows)
    if composer_top is not None:
        # A terminal error is the logical line that *ends at* the current idle
        # composer.  Do not reactivate an older connection error still visible
        # above a later successful response and a new composer.
        candidates = _tail_candidates(rows[:composer_top])
        for candidate in candidates:
            for expression, pattern, provenance in _MID_RESPONSE_TERMINALS:
                if expression.fullmatch(candidate):
                    return _match(
                        pattern=pattern,
                        turn_state=TURN_TERMINAL,
                        recovery_action=ACTION_NUDGE,
                        status=TerminalStatus.ERROR,
                        confidence="high",
                        reason=(
                            f"Claude mid-response terminal line ({provenance}) "
                            "ended at the boxed idle composer"
                        ),
                        raw_text=candidate,
                        idle_composer=True,
                    )
        candidates = [str(rows[composer_top - 1]).strip()] if composer_top else []
    else:
        candidates = [str(line).strip() for line in reversed(rows[-25:])]

    for candidate in candidates:
        if _RETRY_BANNER.fullmatch(candidate):
            return _match(
                pattern="claude.retry-banner",
                turn_state=TURN_SELF_RETRYING,
                recovery_action=ACTION_IGNORE,
                status=TerminalStatus.PROCESSING,
                confidence="high",
                reason="Claude reports an in-progress self-retry; external input is suppressed",
                raw_text=candidate,
            )

    for candidate in candidates:
        if _GENERIC_API_ERROR.fullmatch(candidate):
            return _match(
                pattern="claude.generic-api-error",
                turn_state=TURN_UNKNOWN,
                recovery_action=ACTION_LAYER_2,
                status=None,
                confidence="medium",
                reason=(
                    "an anchored Claude API error is present, but this pattern/action "
                    "has no local executing proof"
                ),
                raw_text=candidate,
            )
    return None


def _active(db: Any, terminal_id: str, generation_key: str) -> Any:
    return (
        db.query(database.ProviderRecoveryEpisodeModel)
        .filter(
            database.ProviderRecoveryEpisodeModel.terminal_id == terminal_id,
            database.ProviderRecoveryEpisodeModel.generation_key == generation_key,
            database.ProviderRecoveryEpisodeModel.active == 1,
        )
        .one_or_none()
    )


def _is_sqlite_contention(exc: OperationalError) -> bool:
    """Whether SQLAlchemy wrapped SQLite's bounded busy/locked condition."""
    original = exc.orig
    if not isinstance(original, sqlite3.OperationalError):
        return False
    code = getattr(original, "sqlite_errorcode", None)
    if isinstance(code, int) and (code & 0xFF) in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    }:
        return True
    message = str(original).lower()
    return any(
        marker in message
        for marker in (
            "database is locked",
            "database table is locked",
            "database schema is locked",
        )
    )


def _published(
    row: Any,
    *,
    native_session_id: Optional[str],
    provider_version: Optional[str],
    agent_id: Optional[str],
    incarnation_id: Optional[str],
) -> dict[str, Any]:
    payload = json.loads(row.match_json)
    if not isinstance(payload, dict):
        raise RecoveryEvidenceUnavailable("stored recovery match is not an object")
    payload.update(
        {
            "occurrence_id": row.occurrence_id,
            "terminal_id": row.terminal_id,
            "generation": row.generation,
            "native_session_id": native_session_id,
            "agent_id": agent_id,
            "incarnation_id": incarnation_id,
            "provider": row.provider,
            "provider_version": provider_version,
            "opened_at": row.opened_at,
        }
    )
    return cast(dict[str, Any], payload)


def identity_context(
    *, terminal_id: str, generation: Optional[str], native_session_id: Optional[str]
) -> dict[str, Optional[str]]:
    """Resolve nullable M3/build identity without inventing missing facts."""
    agent_id: Optional[str] = None
    incarnation_id: Optional[str] = None
    provider_version: Optional[str] = None
    try:
        from cli_agent_orchestrator.services import stable_agent_roster

        incarnation = stable_agent_roster.get_incarnation_by_terminal(
            terminal_id, generation=generation
        )
        if incarnation is not None:
            agent_id = incarnation.get("agent_id")
            incarnation_id = incarnation.get("incarnation_id")
    except Exception as exc:  # noqa: BLE001 - identity is explicitly nullable
        logger.debug("Stable-agent recovery identity unavailable for %s: %s", terminal_id, exc)

    try:
        with database.SessionLocal() as db:
            query = db.query(database.ManagedLaunchV2ReservationModel).filter(
                database.ManagedLaunchV2ReservationModel.terminal_id == terminal_id
            )
            if generation is not None:
                query = query.filter(
                    database.ManagedLaunchV2ReservationModel.generation == generation
                )
            reservation = query.one_or_none()
            raw_binding = (
                cast(Optional[str], reservation.binding_json) if reservation is not None else None
            )
            if raw_binding:
                binding = json.loads(raw_binding)
                value = binding.get("provider_version") if isinstance(binding, dict) else None
                if isinstance(value, str) and value:
                    provider_version = value
            if provider_version is None and generation is not None:
                repair = (
                    db.query(database.NativeStatusRepairEvidenceModel)
                    .filter(
                        database.NativeStatusRepairEvidenceModel.terminal_id == terminal_id,
                        database.NativeStatusRepairEvidenceModel.generation == generation,
                    )
                    .order_by(database.NativeStatusRepairEvidenceModel.observed_at.desc())
                    .first()
                )
                if repair is not None:
                    provider_version = cast(str, repair.provider_version)
    except Exception as exc:  # noqa: BLE001 - build identity is explicitly nullable
        logger.debug("Provider build recovery identity unavailable for %s: %s", terminal_id, exc)

    return {
        "native_session_id": native_session_id,
        "provider_version": provider_version,
        "agent_id": agent_id,
        "incarnation_id": incarnation_id,
    }


def observe(
    *,
    terminal_id: str,
    generation: Optional[str],
    native_session_id: Optional[str],
    provider: str,
    provider_version: Optional[str],
    agent_id: Optional[str],
    incarnation_id: Optional[str],
    screen_lines: Optional[Sequence[str]],
) -> Optional[dict[str, Any]]:
    """Reconcile and publish one exact generation's active detector episode.

    The partial unique index serializes good-faith concurrent status polls.
    A loser retries and adopts the winner's occurrence id; it never creates a
    second active identity for the same generation.
    """
    if not isinstance(terminal_id, str) or not terminal_id:
        raise ValueError("terminal_id must be a non-empty string")
    if not isinstance(provider, str) or not provider:
        raise ValueError("provider must be a non-empty string")
    generation_key = generation or ""
    matched = detect(provider, screen_lines)

    last_error: Optional[Exception] = None
    for attempt in range(len(SQLITE_CONTENTION_RETRY_DELAYS) + 1):
        try:
            with database.SessionLocal() as db:
                now = _now()
                active = _active(db, terminal_id, generation_key)
                if active is not None and (
                    matched is None or active.fingerprint != matched.fingerprint
                ):
                    active.active = 0
                    active.closed_at = now
                    active.last_observed_at = now
                    db.flush()
                    active = None

                if matched is None:
                    db.commit()
                    return None

                if active is None:
                    active = database.ProviderRecoveryEpisodeModel(
                        occurrence_id=str(uuid.uuid4()),
                        terminal_id=terminal_id,
                        generation_key=generation_key,
                        generation=generation,
                        provider=provider,
                        pattern=matched.pattern,
                        fingerprint=matched.fingerprint,
                        match_json=json.dumps(
                            matched.stored_payload(), sort_keys=True, separators=(",", ":")
                        ),
                        active=1,
                        opened_at=now,
                        last_observed_at=now,
                    )
                    db.add(active)
                    db.flush()
                else:
                    active.last_observed_at = now

                result = _published(
                    active,
                    native_session_id=native_session_id,
                    provider_version=provider_version,
                    agent_id=agent_id,
                    incarnation_id=incarnation_id,
                )
                db.commit()
                return result
        except IntegrityError as exc:
            # Another cooperative poll opened the one active slot.  Retry and
            # adopt that durable row rather than minting a duplicate effect id.
            last_error = exc
            continue
        except OperationalError as exc:
            if not _is_sqlite_contention(exc):
                raise RecoveryEvidenceUnavailable(
                    f"provider recovery evidence could not be reconciled: {exc}"
                ) from exc
            # An unrelated cooperative SQLite writer may briefly own the one
            # database write slot. Yield for a tightly bounded interval, then
            # either write this episode or adopt the concurrent winner.
            last_error = exc
            if attempt < len(SQLITE_CONTENTION_RETRY_DELAYS):
                time.sleep(SQLITE_CONTENTION_RETRY_DELAYS[attempt])
            continue
        except Exception as exc:  # noqa: BLE001 - preserve the status surface
            raise RecoveryEvidenceUnavailable(
                f"provider recovery evidence could not be reconciled: {exc}"
            ) from exc
    raise RecoveryEvidenceUnavailable(
        f"provider recovery evidence remained contended after retry: {last_error}"
    )
