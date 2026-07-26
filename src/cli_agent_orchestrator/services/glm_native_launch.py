"""Closed validation helpers for the native GLM route.

The GLM route is deliberately separate from Claude's model vocabulary.  The
Claude validator attests Claude-family observations and must not be widened to
accept a model whose route is provided by the session wrapper.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional

GLM_MODEL_ALLOWLIST = frozenset({"glm-5.2", "glm-5.2[1m]"})
PROVIDER_ROUTE_ANTHROPIC = "anthropic"
PROVIDER_ROUTE_GLM = "glm"
PROVIDER_ROUTES = (PROVIDER_ROUTE_ANTHROPIC, PROVIDER_ROUTE_GLM)


class GlmRouteError(ValueError):
    """A GLM route cannot be honestly attested from the supplied evidence."""


def validate_requested_model(model: Any) -> str:
    """Return an allowlisted GLM model or refuse before provider I/O."""
    if not isinstance(model, str) or model.strip().lower() not in GLM_MODEL_ALLOWLIST:
        raise GlmRouteError(
            f"model {model!r} is not a pinnable GLM route; expected one of "
            f"{sorted(GLM_MODEL_ALLOWLIST)}"
        )
    return model.strip().lower()


def observed_model_matches(requested: str, observed: Optional[str]) -> bool:
    """Require the provider-authored model observation to match byte-for-byte."""
    if not isinstance(observed, str):
        return False
    return observed.strip().lower() == validate_requested_model(requested)


def _canonical_path(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise GlmRouteError(f"route envelope {field} must be a non-empty path")
    if not os.path.isabs(value) or os.path.realpath(value) != value:
        raise GlmRouteError(f"route envelope {field} must be a canonical absolute path")
    return value


def _digest(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise GlmRouteError(f"route envelope {field} must be a lowercase sha256 digest")
    return value


def _executable(path: str, digest: str, field: str) -> None:
    if not os.path.isfile(path) or not os.access(path, os.X_OK):
        raise GlmRouteError(f"route envelope {field} is not an executable file")
    import hashlib

    try:
        observed = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError as exc:
        raise GlmRouteError(f"route envelope {field} is unreadable") from exc
    if observed != digest:
        raise GlmRouteError(f"route envelope {field} digest does not match the file")


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
    """Validate and return the immutable route envelope, or ``None`` for default.

    The route map is checked against the reservation at admission time, so a
    wrapper cannot select a different worktree route than the request that
    owns the generation.  Only path names and route metadata are handled here;
    credential files are never opened.
    """
    if provider_route not in PROVIDER_ROUTES:
        raise GlmRouteError(
            f"unknown provider_route {provider_route!r}; expected {list(PROVIDER_ROUTES)}"
        )
    if provider_route == PROVIDER_ROUTE_ANTHROPIC:
        if envelope is not None:
            raise GlmRouteError("route_envelope is valid only for provider_route='glm'")
        return None
    if provider != "claude_code":
        raise GlmRouteError("provider_route='glm' is supported only for provider=claude_code")
    validate_requested_model(expected_model)
    if not isinstance(envelope, Mapping):
        raise GlmRouteError("provider_route='glm' requires a route_envelope")

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
        "consumed_marker_path": _canonical_path(
            envelope.get("consumed_marker_path"), "consumed_marker_path"
        ),
    }
    if values["worktree_realpath"] != working_directory:
        raise GlmRouteError(
            "route envelope worktree_realpath must equal the reservation working_directory"
        )
    if values["inner_executable"] != provider_executable:
        raise GlmRouteError("route envelope inner_executable must equal provider_executable")
    if values["inner_executable_sha256"] != provider_executable_sha256:
        raise GlmRouteError(
            "route envelope inner_executable_sha256 must equal provider_executable_sha256"
        )
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
        route_map = Path(values["route_map_path"])
        try:
            data = json.loads(route_map.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise GlmRouteError("route map is unreadable or unparseable") from exc
        routes = data.get("routes") if isinstance(data, Mapping) else None
        entry = routes.get(values["worktree_realpath"]) if isinstance(routes, Mapping) else None
        if not isinstance(entry, Mapping):
            raise GlmRouteError("route map has no entry for the reservation worktree")
        if entry.get("route") != PROVIDER_ROUTE_GLM:
            raise GlmRouteError("route map entry is not a GLM route")
        if entry.get("model") != expected_model:
            raise GlmRouteError("route map model does not equal expected_model")
    return values


def consumed_marker_exists(path: str) -> bool:
    """Prove wrapper claim completion without reading or exposing credentials."""
    try:
        return os.path.isfile(path) and Path(path).read_text(encoding="utf-8") == "consumed\n"
    except OSError:
        return False


def validate_session_env(
    *,
    session_env: Any,
    envelope: Mapping[str, str],
    expected_model: str,
) -> dict[str, str]:
    """Validate the reservation's stored session map before a pane exists."""
    if not isinstance(session_env, Mapping) or not session_env:
        raise GlmRouteError("GLM native launch requires a stored session environment")
    required = (
        "CAO_CONDUCTOR_ROUTES",
        "CAO_CONDUCTOR_SHIM_DIR",
        "CAO_CONDUCTOR_REAL_CLAUDE",
    )
    if any(not isinstance(session_env.get(key), str) or not session_env[key] for key in required):
        raise GlmRouteError("stored session environment is missing GLM route bindings")

    route_map = _canonical_path(session_env["CAO_CONDUCTOR_ROUTES"], "session CAO_CONDUCTOR_ROUTES")
    shim_dir = _canonical_path(
        session_env["CAO_CONDUCTOR_SHIM_DIR"], "session CAO_CONDUCTOR_SHIM_DIR"
    )
    real_claude = _canonical_path(
        session_env["CAO_CONDUCTOR_REAL_CLAUDE"], "session CAO_CONDUCTOR_REAL_CLAUDE"
    )
    if route_map != envelope["route_map_path"]:
        raise GlmRouteError("stored session route map differs from the route envelope")
    if real_claude != envelope["inner_executable"]:
        raise GlmRouteError("stored session real Claude differs from the route envelope")
    try:
        if os.path.commonpath([shim_dir, envelope["wrapper_executable"]]) != shim_dir:
            raise GlmRouteError("stored shim dir does not contain the route wrapper")
    except ValueError as exc:
        raise GlmRouteError("stored shim dir and wrapper do not share a path root") from exc

    try:
        data = json.loads(Path(route_map).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GlmRouteError("stored session route map is unreadable or unparseable") from exc
    routes = data.get("routes") if isinstance(data, Mapping) else None
    entry = routes.get(envelope["worktree_realpath"]) if isinstance(routes, Mapping) else None
    if not isinstance(entry, Mapping):
        raise GlmRouteError("stored session route map has no worktree entry")
    if entry.get("route") != PROVIDER_ROUTE_GLM or entry.get("model") != expected_model:
        raise GlmRouteError("stored session route entry does not match the reservation")
    consumed = entry.get("consumed_path")
    if (
        not isinstance(consumed, str)
        or os.path.realpath(consumed) != envelope["consumed_marker_path"]
    ):
        raise GlmRouteError("stored session consumed marker differs from the route envelope")
    return {str(key): str(value) for key, value in session_env.items()}
