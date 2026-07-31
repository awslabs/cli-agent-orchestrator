"""Durable generation-bound structured companion receipts.

Final conformance §20.2f (P1-7/P1-10): the fork's structured companion
surfaces — the §17.2 user-prompt lifecycle, the §18.2 structured refusal
receipt, the §18.9 per-turn route identity, and the §19.5 message-turn
acknowledgement — are observation/receipt only. Records are written ONLY by
provider-native producers (the managed provider bridge and the managed
launch lifecycle), never inferred from pane scraping, inbox ``delivered``
status, or terminal paste.

Every record is bound to the exact terminal/process generation: a stale or
wrong-generation record is never served (readers return None, and the API
answers 204). Receipts carry identities and digests only — message bodies
are never persisted here (redaction).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from cli_agent_orchestrator.constants import COMPANION_DIR

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
_STRICT_ENVELOPE_SCHEMA = "cao-model-turn-receipt-envelope-v1"


class CompanionReceiptInvalid(RuntimeError):
    """A strict receipt store is malformed or internally contradictory."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _record_path(terminal_id: str, generation: str) -> Path:
    safe_terminal = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in terminal_id)
    safe_generation = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in generation)
    return COMPANION_DIR / f"{safe_terminal}-{safe_generation}.json"


def _load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {
            "schema_version": _SCHEMA_VERSION,
            "route": None,
            "prompt": None,
            "refusal": None,
            "message_acks": {},
        }
    if not isinstance(data, dict):
        data = {}
    data.setdefault("schema_version", _SCHEMA_VERSION)
    data.setdefault("route", None)
    data.setdefault("prompt", None)
    data.setdefault("refusal", None)
    data.setdefault("message_acks", {})
    return data


def _reject_duplicate_pairs(pairs):
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise CompanionReceiptInvalid(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _load_strict(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw, object_pairs_hook=_reject_duplicate_pairs)
    except CompanionReceiptInvalid:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CompanionReceiptInvalid("strict companion receipt store is unreadable") from exc
    allowed = {
        "schema_version",
        "route",
        "prompt",
        "refusal",
        "message_acks",
        "terminal_id",
        "generation",
    }
    if (
        not isinstance(data, dict)
        or set(data) - allowed
        or data.get("schema_version") != _SCHEMA_VERSION
    ):
        raise CompanionReceiptInvalid("strict companion receipt store has an unknown shape")
    if not isinstance(data.get("message_acks"), dict):
        raise CompanionReceiptInvalid("strict companion receipt map is malformed")
    return data


def _atomic_write(path: Path, record: dict[str, Any]) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".companion-", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, str(path))
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def _mutate(terminal_id: str, generation: str, mutator) -> None:
    """Read-modify-write the generation's record under an exclusive flock."""
    path = _record_path(terminal_id, generation)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    with open(lock_path, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            record = _load(path)
            record["terminal_id"] = terminal_id
            record["generation"] = generation
            mutator(record)
            _atomic_write(path, record)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _read(terminal_id: str, generation: Optional[str]) -> Optional[dict[str, Any]]:
    """The live record for this exact generation, or None.

    A record file is named by (terminal, generation), so a replacement
    incarnation can never be served a stale record by construction; a corrupt
    or unreadable record fails closed to None (no observation).
    """
    if not generation:
        return None
    path = _record_path(terminal_id, generation)
    if not path.exists():
        return None
    return _load(path)


# -- producers (provider-native only) ------------------------------------------


def record_route_receipt(
    terminal_id: str,
    generation: str,
    *,
    provider: str,
    model: str,
    effort: str,
    receipt_id: str,
    turn_id: str,
    provider_version: Optional[str] = None,
) -> None:
    """Record the per-turn provider-native route identity (§18.9). A new
    turn supersedes any prompt/refusal observed on the prior turn."""

    def mutate(record: dict[str, Any]) -> None:
        record["route"] = {
            "provider": provider,
            "model": model,
            "effort": effort,
            "generation": generation,
            "receipt_id": receipt_id,
            "turn_id": turn_id,
            "provider_version": provider_version,
            "recorded_at": _utcnow(),
        }

    _mutate(terminal_id, generation, mutate)


def record_prompt(
    terminal_id: str,
    generation: str,
    *,
    prompt_id: str,
    text: str,
    choices: list[str],
) -> None:
    """Record a pending provider-native structured user prompt (§17.2)."""

    def mutate(record: dict[str, Any]) -> None:
        record["prompt"] = {
            "prompt_id": prompt_id,
            "text": text,
            "choices": list(choices),
            "generation": generation,
            "recorded_at": _utcnow(),
        }

    _mutate(terminal_id, generation, mutate)


def clear_prompt(terminal_id: str, generation: str, *, prompt_id: str) -> None:
    """Close the prompt lifecycle when the provider-native prompt resolves."""

    def mutate(record: dict[str, Any]) -> None:
        prompt = record.get("prompt")
        if isinstance(prompt, dict) and prompt.get("prompt_id") == prompt_id:
            record["prompt"] = None

    _mutate(terminal_id, generation, mutate)


def record_refusal(
    terminal_id: str,
    generation: str,
    *,
    refusal_id: str,
    identity: str,
    turn_id: str,
) -> None:
    """Record a provider-native structured refusal receipt (§18.2)."""

    def mutate(record: dict[str, Any]) -> None:
        record["refusal"] = {
            "refusal_id": refusal_id,
            "identity": identity,
            "turn_id": turn_id,
            "generation": generation,
            "recorded_at": _utcnow(),
        }

    _mutate(terminal_id, generation, mutate)


def record_message_ack(
    terminal_id: str,
    generation: str,
    *,
    message_id: Any,
    ack: dict[str, Any],
) -> None:
    """Record the provider-native ``terminal_queued → submitted`` message-turn
    acknowledgement (§19.5/P1-7). Exactly-once per message id: a replay of the
    same acknowledgement is a no-op, and a conflicting re-acknowledgement of
    the same message id is never written."""

    key = str(message_id)

    def mutate(record: dict[str, Any]) -> None:
        acks = record.setdefault("message_acks", {})
        if key in acks:
            return  # legacy envelopes retain their historical first-write rule
        if ack.get("schema") == "cao-model-turn-receipt-v1":
            from cli_agent_orchestrator.services import model_turn_receipt_contract

            strict = model_turn_receipt_contract.validate_receipt(ack)
            acks[key] = {
                "schema": _STRICT_ENVELOPE_SCHEMA,
                "receipt": strict,
                "recorded_at": _utcnow(),
            }
        else:
            acks[key] = {**ack, "recorded_at": _utcnow()}

    if ack.get("schema") != "cao-model-turn-receipt-v1":
        _mutate(terminal_id, generation, mutate)
        return

    from cli_agent_orchestrator.services import model_turn_receipt_contract

    strict = model_turn_receipt_contract.validate_receipt(ack)
    path = _record_path(terminal_id, generation)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    with open(lock_path, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if path.exists():
                record = _load_strict(path)
            else:
                record = {
                    "schema_version": _SCHEMA_VERSION,
                    "route": None,
                    "prompt": None,
                    "refusal": None,
                    "message_acks": {},
                }
            for field, expected in (
                ("terminal_id", terminal_id),
                ("generation", generation),
            ):
                observed = record.get(field)
                if observed not in (None, expected):
                    raise CompanionReceiptInvalid(f"strict companion receipt store {field} changed")
                record[field] = expected
            acks = record["message_acks"]
            existing = acks.get(key)
            if existing is not None:
                if (
                    not isinstance(existing, dict)
                    or set(existing) != {"schema", "receipt", "recorded_at"}
                    or existing.get("schema") != _STRICT_ENVELOPE_SCHEMA
                    or existing.get("receipt") != strict
                    or not isinstance(existing.get("recorded_at"), str)
                    or not existing["recorded_at"].endswith("Z")
                ):
                    raise CompanionReceiptInvalid(
                        "strict acknowledgement replay contradicts stored envelope"
                    )
                try:
                    datetime.fromisoformat(existing["recorded_at"].removesuffix("Z") + "+00:00")
                except ValueError as exc:
                    raise CompanionReceiptInvalid(
                        "strict acknowledgement envelope timestamp is malformed"
                    ) from exc
                return
            acks[key] = {
                "schema": _STRICT_ENVELOPE_SCHEMA,
                "receipt": strict,
                "recorded_at": _utcnow(),
            }
            _atomic_write(path, record)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


# -- readers (generation-bound; stale/wrong generation is never served) --------


def get_prompt(terminal_id: str, generation: Optional[str]) -> Optional[dict[str, Any]]:
    record = _read(terminal_id, generation)
    if record is None:
        return None
    prompt = record.get("prompt")
    return prompt if isinstance(prompt, dict) else None


def get_refusal(terminal_id: str, generation: Optional[str]) -> Optional[dict[str, Any]]:
    record = _read(terminal_id, generation)
    if record is None:
        return None
    refusal = record.get("refusal")
    return refusal if isinstance(refusal, dict) else None


def get_route(terminal_id: str, generation: Optional[str]) -> Optional[dict[str, Any]]:
    record = _read(terminal_id, generation)
    if record is None:
        return None
    route = record.get("route")
    return route if isinstance(route, dict) else None


def get_message_ack(
    terminal_id: str, generation: Optional[str], message_id: Any
) -> Optional[dict[str, Any]]:
    record = _read(terminal_id, generation)
    if record is None:
        return None
    ack = (record.get("message_acks") or {}).get(str(message_id))
    if not isinstance(ack, dict):
        return None
    if ack.get("schema") == _STRICT_ENVELOPE_SCHEMA:
        receipt = ack.get("receipt")
        return receipt if isinstance(receipt, dict) else None
    return ack


def get_strict_message_ack(
    terminal_id: str, generation: Optional[str], message_id: Any
) -> Optional[dict[str, str]]:
    """Return the exact 14-field receipt, never its storage metadata envelope."""
    if not generation:
        return None
    path = _record_path(terminal_id, generation)
    if not path.exists():
        return None
    record = _load_strict(path)
    if record.get("terminal_id") != terminal_id or record.get("generation") != generation:
        raise CompanionReceiptInvalid("strict companion receipt store identity is malformed")
    stored = record["message_acks"].get(str(message_id))
    if stored is None:
        return None
    if (
        not isinstance(stored, dict)
        or set(stored) != {"schema", "receipt", "recorded_at"}
        or stored.get("schema") != _STRICT_ENVELOPE_SCHEMA
        or not isinstance(stored.get("recorded_at"), str)
        or not stored["recorded_at"]
    ):
        raise CompanionReceiptInvalid("strict receipt storage envelope is malformed")
    from cli_agent_orchestrator.services import model_turn_receipt_contract

    try:
        recorded_at = stored["recorded_at"]
        if not recorded_at.endswith("Z"):
            raise ValueError("recorded_at is not canonical UTC Z")
        recorded = datetime.fromisoformat(recorded_at.removesuffix("Z") + "+00:00")
        receipt = model_turn_receipt_contract.validate_receipt(stored.get("receipt"))
        submitted = datetime.fromisoformat(receipt["submitted_at"].removesuffix("Z") + "+00:00")
        if recorded < submitted:
            raise ValueError("receipt envelope predates provider submission")
        return receipt
    except (
        ValueError,
        model_turn_receipt_contract.ReceiptValidationError,
    ) as exc:
        raise CompanionReceiptInvalid("stored strict receipt is invalid") from exc
