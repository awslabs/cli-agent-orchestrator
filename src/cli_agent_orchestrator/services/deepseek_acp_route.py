"""Closed validation helpers for the managed DeepSeek ACP route.

DeepSeek managed launches run the pinned real Claude binary on the
Anthropic-compatible DeepSeek gateway.  The gateway credential is a
one-shot token held in a conductor-owned file that only the pinned
wrapper may claim; the fork proves the *topology* (wrapper/inner
identities, route map, token present, consumed marker absent) before any
provider I/O and never reads the token bytes.  This module is the
DeepSeek sibling of ``glm_native_launch`` and is deliberately separate:
the GLM validator attests a native Z.ai route and its own model
vocabulary, and widening either to cover the other would let one
provider's drift be accepted under the other's name.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Mapping, Optional

DEEPSEEK_MODEL_ALLOWLIST = frozenset({"deepseek-v4-flash", "deepseek-v4-pro"})
PROVIDER_ROUTE_ANTHROPIC = "anthropic"
PROVIDER_ROUTE_DEEPSEEK = "deepseek"
PROVIDER_ROUTES = (PROVIDER_ROUTE_ANTHROPIC, PROVIDER_ROUTE_DEEPSEEK)

#: The route-map shape the conductor writes for a DeepSeek worktree entry
#: (``routes`` keyed by worktree realpath; each entry carries ``route``,
#: ``model``, ``token_path`` and ``consumed_path``).  Documented here for
#: fixtures and consumers; the live conductor map carries no schema field,
#: so validation keys on the entry fields rather than this label.
ROUTE_MAP_SCHEMA = "cao-conductor-deepseek-route-map-v1"

#: The consumed marker's exact bytes.  The wrapper writes these only after
#: the one-shot token file is unlinked, so the marker implies the token is
#: gone — and a marker that existed before a launch is a replay of an
#: already-consumed token, refused with zero provider I/O.
CONSUMED_MARKER_BYTES = "consumed\n"


class DeepSeekRouteError(ValueError):
    """A DeepSeek route cannot be honestly attested from the supplied evidence."""


def validate_requested_model(model: Any) -> str:
    """Return an allowlisted DeepSeek model or refuse before provider I/O."""
    if not isinstance(model, str) or model.strip().lower() not in DEEPSEEK_MODEL_ALLOWLIST:
        raise DeepSeekRouteError(
            f"model {model!r} is not a pinnable DeepSeek route; expected one of "
            f"{sorted(DEEPSEEK_MODEL_ALLOWLIST)}"
        )
    return model.strip().lower()


def observed_model_matches(requested: str, observed: Optional[str]) -> bool:
    """Require the provider-authored model observation to match byte-for-byte."""
    if not isinstance(observed, str):
        return False
    return observed.strip().lower() == validate_requested_model(requested)


def _canonical_path(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise DeepSeekRouteError(f"route envelope {field} must be a non-empty path")
    if not os.path.isabs(value) or os.path.realpath(value) != value:
        raise DeepSeekRouteError(f"route envelope {field} must be a canonical absolute path")
    return value


def _digest(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise DeepSeekRouteError(f"route envelope {field} must be a lowercase sha256 digest")
    return value


def _executable(path: str, digest: str, field: str) -> None:
    if not os.path.isfile(path) or not os.access(path, os.X_OK):
        raise DeepSeekRouteError(f"route envelope {field} is not an executable file")
    import hashlib

    try:
        observed = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError as exc:
        raise DeepSeekRouteError(f"route envelope {field} is unreadable") from exc
    if observed != digest:
        raise DeepSeekRouteError(f"route envelope {field} digest does not match the file")


def token_present(path: str) -> bool:
    """True iff the one-shot token is a real, owner-owned, non-symlink file.

    The wrapper claims the token by atomic rename; a symlink or a
    directory here means somebody substituted the token surface, and the
    launch must fail closed rather than hand the wrapper foreign bytes.
    """
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()


def consumed_marker_exists(path: str) -> bool:
    """Prove wrapper claim completion without reading or exposing credentials."""
    try:
        return Path(path).read_text(encoding="utf-8") == CONSUMED_MARKER_BYTES
    except OSError:
        return False


def _validate_route_map(
    route_map_path: str,
    worktree_realpath: str,
    expected_model: str,
    token_path: str,
    consumed_marker_path: str,
) -> None:
    """Cross-check the conductor's route map against the pinned envelope.

    Only path names and route metadata are compared; token contents are
    never opened.  The route map is the conductor's live authority, so a
    wrapper can only claim the token the reservation's own worktree was
    registered for.
    """
    try:
        data = json.loads(Path(route_map_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DeepSeekRouteError("route map is unreadable or unparseable") from exc
    routes = data.get("routes") if isinstance(data, Mapping) else None
    entry = routes.get(worktree_realpath) if isinstance(routes, Mapping) else None
    if not isinstance(entry, Mapping):
        raise DeepSeekRouteError("route map has no entry for the reservation worktree")
    if entry.get("route") != PROVIDER_ROUTE_DEEPSEEK:
        raise DeepSeekRouteError("route map entry is not a deepseek route")
    if entry.get("model") != expected_model:
        raise DeepSeekRouteError("route map model does not equal expected_model")
    entry_token = entry.get("token_path")
    if not isinstance(entry_token, str) or os.path.realpath(entry_token) != token_path:
        raise DeepSeekRouteError("route map token_path differs from the route envelope")
    entry_consumed = entry.get("consumed_path")
    if (
        not isinstance(entry_consumed, str)
        or os.path.realpath(entry_consumed) != consumed_marker_path
    ):
        raise DeepSeekRouteError("route map consumed_path differs from the route envelope")


def validate_envelope(
    *,
    provider: str,
    provider_route: Any,
    expected_model: str,
    working_directory: str,
    provider_executable: str,
    provider_executable_sha256: str,
    envelope: Any,
    check_files: bool = True,
) -> Optional[dict[str, str]]:
    """Validate and return the immutable DeepSeek route envelope.

    ``None`` for the default Anthropic route.  For ``deepseek`` the
    envelope must pin wrapper + inner identities and the exact
    token/marker topology: token present, consumed marker absent — a
    marker already on disk means a previous launch consumed this
    worktree's one-shot token and the replay must be refused before any
    provider byte exists.
    """
    if provider_route not in PROVIDER_ROUTES:
        raise DeepSeekRouteError(
            f"unknown provider_route {provider_route!r}; expected {list(PROVIDER_ROUTES)}"
        )
    if provider_route == PROVIDER_ROUTE_ANTHROPIC:
        if envelope is not None:
            raise DeepSeekRouteError("route_envelope is valid only for provider_route='deepseek'")
        return None
    if provider != "claude_code":
        raise DeepSeekRouteError(
            "provider_route='deepseek' is supported only for provider=claude_code"
        )
    validate_requested_model(expected_model)
    if not isinstance(envelope, Mapping):
        raise DeepSeekRouteError("provider_route='deepseek' requires a route_envelope")

    values = {
        "wrapper_executable": _canonical_path(
            envelope.get("wrapper_executable"), "wrapper_executable"
        ),
        "wrapper_executable_sha256": _digest(
            envelope.get("wrapper_executable_sha256"), "wrapper_executable_sha256"
        ),
        "inner_executable": _canonical_path(envelope.get("inner_executable"), "inner_executable"),
        "inner_executable_sha256": _digest(
            envelope.get("inner_executable_sha256"), "inner_executable_sha256"
        ),
        "route_map_path": _canonical_path(envelope.get("route_map_path"), "route_map_path"),
        "worktree_realpath": _canonical_path(
            envelope.get("worktree_realpath"), "worktree_realpath"
        ),
        "token_path": _canonical_path(envelope.get("token_path"), "token_path"),
        "consumed_marker_path": _canonical_path(
            envelope.get("consumed_marker_path"), "consumed_marker_path"
        ),
    }
    if values["worktree_realpath"] != working_directory:
        raise DeepSeekRouteError(
            "route envelope worktree_realpath must equal the reservation working_directory"
        )
    if values["inner_executable"] != provider_executable:
        raise DeepSeekRouteError("route envelope inner_executable must equal provider_executable")
    if values["inner_executable_sha256"] != provider_executable_sha256:
        raise DeepSeekRouteError(
            "route envelope inner_executable_sha256 must equal provider_executable_sha256"
        )
    if os.path.realpath(values["token_path"]) == os.path.realpath(values["consumed_marker_path"]):
        raise DeepSeekRouteError("route envelope token and consumed marker must be distinct paths")
    if check_files:
        _executable(
            values["wrapper_executable"],
            values["wrapper_executable_sha256"],
            "wrapper_executable",
        )
        _executable(
            values["inner_executable"],
            values["inner_executable_sha256"],
            "inner_executable",
        )
        _validate_route_map(
            values["route_map_path"],
            values["worktree_realpath"],
            expected_model,
            values["token_path"],
            values["consumed_marker_path"],
        )
        if not token_present(values["token_path"]):
            raise DeepSeekRouteError(
                "route envelope token is not a present owner-owned regular file"
            )
        if consumed_marker_exists(values["consumed_marker_path"]) or os.path.lexists(
            values["consumed_marker_path"]
        ):
            raise DeepSeekRouteError(
                "route envelope consumed marker already exists; the one-shot token "
                "was already consumed (replay refused)"
            )
    return values
