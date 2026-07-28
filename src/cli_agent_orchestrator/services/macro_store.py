"""Durable operator macro library (§5.1–§5.4).

A small versioned JSON store at ``CAO_HOME_DIR/macros.json`` (inheriting the
``CAO_STATE_ROOT`` relocation, §1.6).  The design decisions this implements:

- **D5 — a versioned JSON store, not a settings section.**  The deployed
  ``settings.json`` is unversioned and non-atomic (Finding F1); this store
  has ``schema_version``, flock-serialized read-modify-write, and
  ``mkstemp`` + ``os.replace`` atomic writes at mode ``0600`` (the
  receipt-writer precedent, ``services/wake_receipts.py:135-145``).
- **The v3 event array is the correctness boundary.**  Every record is
  validated through the contract's ``normalize_sequence_events`` on load
  and on every write.  Notation never touches disk.
- **Nothing is ever silently dropped.**  A newer ``schema_version``,
  unparseable JSON, or a top-level shape violation moves the whole file
  aside to ``macros.quarantine-<utc-isots>.json`` and the store starts
  empty; a per-record validation failure appends that record to the
  quarantine file and drops it from the working set.  Every list response
  reports the quarantine until the operator deletes the file.  The server
  never fails to start over a macro file (loads are lazy, one per
  operation, under the flock).
- **Only the CAO server writes this file.**  Built-ins are synthesized at
  read time from the §4 provider-control registry (D6, below) and never
  persisted.

Scope discipline matches the settings routes: READ for list, WRITE for
mutations; the routes live in ``api/main.py``.
"""

import contextlib
import fcntl
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar

_T = TypeVar("_T")

from cli_agent_orchestrator.constants import CAO_HOME_DIR
from cli_agent_orchestrator.services.control_input_contract import (
    normalize_sequence_events,
)
from cli_agent_orchestrator.services.macro_notation import parse_notation
from cli_agent_orchestrator.services.provider_controls import (
    advertised_provider_controls,
)

SCHEMA_VERSION = 1

MACROS_PATH = CAO_HOME_DIR / "macros.json"

# Scope ranks for the pinned server-side ordering (§5.4): favorites first,
# then by scope rank global → provider → profile, then case-insensitive
# name.  Built-ins resolve in the provider scope group (§5.5).
_SCOPE_RANKS = {"global": 0, "provider": 1, "profile": 2}

# ── Built-in synthesis (§5.5, D6) ────────────────────────────────────────
# The §4 provider-control registry (services/provider_controls.py) is the
# sole provider-control authority: built-ins are synthesized from its
# advertised entries at read time and never persisted, so immutability is
# structural and the data can never drift from the version-pinned evidence.
# IDs are deterministic and namespaced (§5.5): ``builtin:<provider>:<kind>``;
# the ``builtin:`` prefix is reserved (user-record IDs are UUIDs).
BUILTIN_ID_PREFIX = "builtin:"

_BUILTIN_LABELS = {
    "compact": {"name": "Compact", "description": "Provider-native /compact"},
    "stop": {"name": "Stop", "description": "Interrupt the current turn (Escape)"},
}


def builtin_macro_id(provider: str, kind: str) -> str:
    """The deterministic built-in ID: ``builtin:<provider>:<kind>``."""
    return f"{BUILTIN_ID_PREFIX}{provider}:{kind}"


def builtin_macros_for_provider(provider: Optional[str]) -> List[Dict[str, Any]]:
    """Synthesize the §5.5 built-ins for one provider from the registry.

    Each record carries ``origin: "builtin"``, ``mutable: False``,
    ``favorite: True`` (built-ins sort first in resolution order and cannot
    be un-favorited — duplicating one makes a user macro that can), and the
    provider scope group.  A provider with no registry entry yields nothing;
    the dashboard hides the built-ins and states why (§13, OD3).
    """
    if not provider:
        return []
    controls = advertised_provider_controls().get(provider)
    if not controls:
        return []
    builtins: List[Dict[str, Any]] = []
    for kind in ("compact", "stop"):
        block = controls.get(kind)
        if block is None:
            continue
        labels = _BUILTIN_LABELS[kind]
        builtins.append(
            {
                "id": builtin_macro_id(provider, kind),
                "name": labels["name"],
                "description": labels["description"],
                "scope": {"kind": "provider", "provider": provider},
                "events": [dict(event) for event in block["events"]],
                "favorite": True,
                "origin": "builtin",
                "mutable": False,
                "builtin_kind": kind,
                "created_at": None,
                "updated_at": None,
            }
        )
    return builtins


def resolve_builtin(macro_id: str) -> Optional[Dict[str, Any]]:
    """Resolve a deterministic built-in ID back to its synthesized record.

    ``POST /macros/{id}/duplicate`` uses this so a built-in id fetched from a
    list response resolves to the same built-in at duplicate time (§5.5 ID
    stability).  Unknown or malformed ids return ``None``.
    """
    if not isinstance(macro_id, str) or not macro_id.startswith(BUILTIN_ID_PREFIX):
        return None
    remainder = macro_id[len(BUILTIN_ID_PREFIX) :]
    provider, sep, kind = remainder.rpartition(":")
    if not sep or not provider or kind not in _BUILTIN_LABELS:
        return None
    for record in builtin_macros_for_provider(provider):
        if record["id"] == macro_id:
            return record
    return None


class MacroValidationError(ValueError):
    """A create/update payload failed validation; carries §5.4 422 errors."""

    def __init__(self, errors: List[Dict[str, Any]]) -> None:
        super().__init__("; ".join(error["message"] for error in errors))
        self.errors = errors


class BuiltinMacroConflictError(ValueError):
    """A mutation targeted a built-in id (§5.4: PUT/DELETE → 409)."""


class MacroNotFoundError(KeyError):
    """No user record (or built-in, for duplicate) carries this id."""


def _utc_isots() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _lock_path() -> Path:
    return MACROS_PATH.parent / f"{MACROS_PATH.name}.lock"


def _quarantine_paths() -> List[Path]:
    """Existing quarantine files, oldest first (timestamped names sort)."""
    return sorted(MACROS_PATH.parent.glob("macros.quarantine-*.json"))


def _new_quarantine_path() -> Path:
    stamp = _utc_isots().replace(":", "-")
    return MACROS_PATH.parent / f"macros.quarantine-{stamp}.json"


def _atomic_write(path: Path, document: Dict[str, Any]) -> None:
    """mkstemp + os.replace at mode 0600 (wake_receipts precedent)."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".macros-", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, str(path))
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def _validate_scope(scope: Any) -> Dict[str, Any]:
    """Normalize one scope to exactly one of the three pinned kinds."""
    if not isinstance(scope, dict):
        raise ValueError("scope must be an object")
    kind = scope.get("kind")
    if kind == "global":
        return {"kind": "global"}
    if kind == "provider":
        provider = scope.get("provider")
        if not isinstance(provider, str) or not provider:
            raise ValueError("a provider scope requires a non-empty string 'provider'")
        return {"kind": "provider", "provider": provider}
    if kind == "profile":
        profile = scope.get("profile")
        if not isinstance(profile, str) or not profile:
            raise ValueError("a profile scope requires a non-empty string 'profile'")
        return {"kind": "profile", "profile": profile}
    raise ValueError(f"unknown scope kind {kind!r}")


def _validate_record(record: Any) -> Dict[str, Any]:
    """Validate one stored record; raise ValueError naming the failure.

    The pinned per-record failure modes (§5.2): bad events, unknown scope
    kind, missing id/name — plus the type checks that keep the file the
    pinned shape.  A user id carrying the reserved ``builtin:`` prefix is
    invalid: built-ins are synthesized, never persisted (§5.5).
    """
    if not isinstance(record, dict):
        raise ValueError("a macro record must be an object")
    record_id = record.get("id")
    if not isinstance(record_id, str) or not record_id:
        raise ValueError("a macro record requires a non-empty string 'id'")
    if record_id.startswith(BUILTIN_ID_PREFIX):
        raise ValueError(
            f"user record id {record_id!r} uses the reserved "
            f"{BUILTIN_ID_PREFIX!r} prefix; built-ins are synthesized, never "
            "persisted"
        )
    name = record.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("a macro record requires a non-empty string 'name'")
    description = record.get("description")
    if description is not None and not isinstance(description, str):
        raise ValueError("'description' must be a string when present")
    scope = _validate_scope(record.get("scope"))
    events = record.get("events")
    try:
        normalized_events = normalize_sequence_events(events)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid events: {exc}") from exc
    favorite = record.get("favorite")
    if not isinstance(favorite, bool):
        raise ValueError("'favorite' must be a boolean")
    created_at = record.get("created_at")
    updated_at = record.get("updated_at")
    for field, value in (("created_at", created_at), ("updated_at", updated_at)):
        if value is not None and not isinstance(value, str):
            raise ValueError(f"'{field}' must be an ISO timestamp string when present")
    return {
        "id": record_id,
        "name": name,
        "description": description,
        "scope": scope,
        "events": normalized_events,
        "favorite": favorite,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _append_to_quarantine(dropped: List[Any]) -> None:
    """Append per-record failures to the quarantine file (§5.2).

    The quarantine file the operator deletes is one JSON document listing
    the raw dropped records; a moved-aside whole file keeps its original
    bytes, so records from a *different* load land in a fresh timestamped
    file rather than corrupting that evidence.
    """
    if not dropped:
        return
    existing = _quarantine_paths()
    target: Optional[Path] = None
    records: List[Any] = []
    if existing:
        candidate = existing[-1]
        try:
            document = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            document = None
        if isinstance(document, dict) and isinstance(document.get("records"), list):
            target = candidate
            records = document["records"]
    if target is None:
        target = _new_quarantine_path()
    records.extend(dropped)
    _atomic_write(
        target,
        {
            "quarantined_at": _utc_isots(),
            "reason": "per-record validation failure",
            "records": records,
        },
    )


def _quarantine_whole_file(reason: str) -> None:
    """Move an unloadable macros.json aside; the store starts empty (§5.2)."""
    target = _new_quarantine_path()
    with contextlib.suppress(FileNotFoundError):
        os.replace(str(MACROS_PATH), str(target))


def _load_document() -> Dict[str, Any]:
    """Load the working set under no lock (callers hold the flock).

    Returns ``{"schema_version": 1, "macros": [validated records]}``.
    Quarantine side effects happen here, exactly once per load: whole-file
    violations move the file aside; per-record failures are appended to the
    quarantine file and dropped.  Nothing is ever silently dropped.
    """
    if not MACROS_PATH.exists():
        return {"schema_version": SCHEMA_VERSION, "macros": []}
    try:
        raw = json.loads(MACROS_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        _quarantine_whole_file("unparseable JSON")
        return {"schema_version": SCHEMA_VERSION, "macros": []}
    if not isinstance(raw, dict) or not isinstance(raw.get("macros"), list):
        _quarantine_whole_file("top-level shape violation")
        return {"schema_version": SCHEMA_VERSION, "macros": []}
    version = raw.get("schema_version")
    if not isinstance(version, int) or version > SCHEMA_VERSION:
        # A newer schema is not ours to read; there is no older version on
        # this base to migrate from.  Version upgrades migrate in place
        # where lossless (§5.2) — the migration table hangs off this branch
        # when a schema_version 2 ever exists.
        _quarantine_whole_file(f"unsupported schema_version {version!r}")
        return {"schema_version": SCHEMA_VERSION, "macros": []}
    records: List[Dict[str, Any]] = []
    dropped: List[Any] = []
    for entry in raw["macros"]:
        try:
            records.append(_validate_record(entry))
        except ValueError:
            dropped.append(entry)
    _append_to_quarantine(dropped)
    return {"schema_version": SCHEMA_VERSION, "macros": records}


def _quarantine_report() -> Optional[Dict[str, Any]]:
    """The ``quarantine`` block of a list response, while a file exists.

    Reported on every list until the operator deletes the quarantine file
    (§5.2).  ``count`` is the number of records known to be inside; an
    unparseable moved-aside file reports ``None`` — the file itself is the
    evidence and its raw bytes cannot be record-counted.
    """
    paths = _quarantine_paths()
    if not paths:
        return None
    latest = paths[-1]
    count: Optional[int] = None
    try:
        document = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        document = None
    if isinstance(document, dict) and isinstance(document.get("records"), list):
        count = len(document["records"])
    elif isinstance(document, dict) and isinstance(document.get("macros"), list):
        count = len(document["macros"])
    elif isinstance(document, list):
        count = len(document)
    return {"count": count, "path": str(latest)}


def _visible(record: Dict[str, Any], provider: Optional[str], profile: Optional[str]) -> bool:
    """The §5.4 visible set: global, provider-matching, or profile-matching."""
    scope = record["scope"]
    kind = scope["kind"]
    if kind == "global":
        return True
    if kind == "provider":
        return provider is not None and scope["provider"] == provider
    return profile is not None and scope["profile"] == profile


def _sort_key(index_and_record: "tuple[int, Dict[str, Any]]") -> "tuple":
    index, record = index_and_record
    return (
        0 if record["favorite"] else 1,
        _SCOPE_RANKS[record["scope"]["kind"]],
        record["name"].casefold(),
        index,
    )


def _annotate_user(record: Dict[str, Any]) -> Dict[str, Any]:
    return {**record, "origin": "user", "mutable": True}


def list_macros(provider: Optional[str] = None, profile: Optional[str] = None) -> Dict[str, Any]:
    """The §5.4 visible set with the pinned server-side ordering.

    Favorites first (built-ins are favorites and sort first, §5.5), then by
    scope rank global → provider → profile, then case-insensitive name;
    ties keep file order.  Includes ``quarantine`` while a quarantine file
    exists.
    """
    _lock_path().parent.mkdir(parents=True, exist_ok=True)
    with open(_lock_path(), "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            document = _load_document()
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    visible: List[Dict[str, Any]] = [
        _annotate_user(record)
        for record in document["macros"]
        if _visible(record, provider, profile)
    ]
    visible.extend(builtin_macros_for_provider(provider))
    ordered = [
        record
        for _, record in sorted(enumerate(visible), key=lambda pair: _sort_key((pair[0], pair[1])))
    ]
    response: Dict[str, Any] = {"macros": ordered}
    quarantine = _quarantine_report()
    if quarantine is not None:
        response["quarantine"] = quarantine
    return response


def _resolve_events(
    events: Any, notation: Any, errors: List[Dict[str, Any]]
) -> Optional[List[Dict[str, Any]]]:
    """The §5.4 either/or: exactly one of ``events`` / ``notation``.

    Notation parses through the §5.3 authority (server-side, never the
    client preview); raw events validate through contract normalization.
    """
    has_events = events is not None
    has_notation = notation is not None
    if has_events == has_notation:
        errors.append(
            {
                "offset": None,
                "message": "name exactly one of 'events' or 'notation'",
            }
        )
        return None
    if has_notation:
        # The §5.3 authority parses; its failures are already in the
        # (offset, message) shape the route reports.
        result = parse_notation(notation)
        if result.errors:
            errors.extend(
                {"offset": error.offset, "message": error.message} for error in result.errors
            )
            return None
        assert result.events is not None
        return result.events
    try:
        return normalize_sequence_events(events)
    except (TypeError, ValueError) as exc:
        errors.append({"offset": None, "message": f"invalid events: {exc}"})
        return None


def _validate_mutable_fields(
    name: Any,
    description: Any,
    scope: Any,
    favorite: Any,
    errors: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Validate the mutable fields of a create/update (full replace)."""
    fields: Dict[str, Any] = {}
    if not isinstance(name, str) or not name:
        errors.append({"offset": None, "message": "a macro requires a non-empty string 'name'"})
    else:
        fields["name"] = name
    if description is not None and not isinstance(description, str):
        errors.append({"offset": None, "message": "'description' must be a string when present"})
    else:
        fields["description"] = description
    try:
        fields["scope"] = _validate_scope(scope)
    except ValueError as exc:
        errors.append({"offset": None, "message": str(exc)})
    if favorite is None:
        fields["favorite"] = False
    elif not isinstance(favorite, bool):
        errors.append({"offset": None, "message": "'favorite' must be a boolean when present"})
    else:
        fields["favorite"] = favorite
    return fields


def _mutate(mutator: Callable[[Dict[str, Any]], _T]) -> _T:
    """Run one read-modify-write under the exclusive flock (§5.1)."""
    _lock_path().parent.mkdir(parents=True, exist_ok=True)
    with open(_lock_path(), "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            document = _load_document()
            result = mutator(document)
            _atomic_write(MACROS_PATH, document)
            return result
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _find_user_record(document: Dict[str, Any], macro_id: str) -> Dict[str, Any]:
    if macro_id.startswith(BUILTIN_ID_PREFIX):
        raise BuiltinMacroConflictError(
            f"{macro_id!r} is a synthesized built-in; built-ins are immutable — "
            "duplicate it to edit a copy"
        )
    records: List[Dict[str, Any]] = document["macros"]
    for record in records:
        if record["id"] == macro_id:
            return record
    raise MacroNotFoundError(macro_id)


def create_macro(
    *,
    name: Any,
    description: Any = None,
    scope: Any,
    events: Any = None,
    notation: Any = None,
    favorite: Any = None,
) -> Dict[str, Any]:
    """Create one user record (§5.4 POST /macros)."""
    errors: List[Dict[str, Any]] = []
    resolved_events = _resolve_events(events, notation, errors)
    fields = _validate_mutable_fields(name, description, scope, favorite, errors)
    if errors:
        raise MacroValidationError(errors)
    assert resolved_events is not None
    now = _utc_isots()
    record = {
        "id": str(uuid.uuid4()),
        "created_at": now,
        "updated_at": now,
        **fields,
        "events": resolved_events,
    }

    def _add(document: Dict[str, Any]) -> Dict[str, Any]:
        document["macros"].append(record)
        return record

    return _mutate(_add)


def update_macro(
    macro_id: str,
    *,
    name: Any,
    description: Any = None,
    scope: Any,
    events: Any = None,
    notation: Any = None,
    favorite: Any = None,
) -> Dict[str, Any]:
    """Full replace of one user record's mutable fields (§5.4 PUT).

    Built-in ids conflict (409); unknown ids are not found (404).
    ``updated_at`` bumps; ``created_at`` and the id are immutable.
    """
    errors: List[Dict[str, Any]] = []
    resolved_events = _resolve_events(events, notation, errors)
    fields = _validate_mutable_fields(name, description, scope, favorite, errors)
    if errors:
        raise MacroValidationError(errors)
    assert resolved_events is not None

    def _replace(document: Dict[str, Any]) -> Dict[str, Any]:
        record = _find_user_record(document, macro_id)
        record.update(fields)
        record["events"] = resolved_events
        record["updated_at"] = _utc_isots()
        return record

    return _mutate(_replace)


def delete_macro(macro_id: str) -> Dict[str, Any]:
    """Delete one user record (§5.4 DELETE). Built-ins conflict (409)."""

    def _remove(document: Dict[str, Any]) -> Dict[str, Any]:
        record = _find_user_record(document, macro_id)
        document["macros"] = [entry for entry in document["macros"] if entry["id"] != macro_id]
        return {"deleted": record["id"]}

    return _mutate(_remove)


def duplicate_macro(macro_id: str, *, name: Any = None) -> Dict[str, Any]:
    """Mint a user record from any source (§5.4 POST …/duplicate).

    The only way to "edit" a built-in: the source may be a built-in id
    (resolved deterministically, §5.5) or a user id.  The copy gets a fresh
    UUID, keeps the source's scope/events/favorite, and takes the given
    name (default: the source name).
    """

    def _copy(document: Dict[str, Any]) -> Dict[str, Any]:
        source: Optional[Dict[str, Any]] = None
        if macro_id.startswith(BUILTIN_ID_PREFIX):
            source = resolve_builtin(macro_id)
            if source is None:
                raise MacroNotFoundError(macro_id)
        else:
            for record in document["macros"]:
                if record["id"] == macro_id:
                    source = record
                    break
            if source is None:
                raise MacroNotFoundError(macro_id)
        errors: List[Dict[str, Any]] = []
        new_name = name if name is not None else source["name"]
        if not isinstance(new_name, str) or not new_name:
            errors.append({"offset": None, "message": "a macro requires a non-empty string 'name'"})
            raise MacroValidationError(errors)
        now = _utc_isots()
        record = {
            "id": str(uuid.uuid4()),
            "name": new_name,
            "description": source.get("description"),
            "scope": dict(source["scope"]),
            "events": [dict(event) for event in source["events"]],
            "favorite": bool(source.get("favorite")),
            "created_at": now,
            "updated_at": now,
        }
        document["macros"].append(record)
        return record

    return _mutate(_copy)
