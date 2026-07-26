"""Closed-route proofs for the native GLM reservation envelope."""

from __future__ import annotations

import hashlib
import json

import pytest

from cli_agent_orchestrator.services import glm_native_launch as glm
from cli_agent_orchestrator.services import managed_provider_bridge as bridge


def _executable(path, contents: str) -> str:
    path.write_text(contents)
    path.chmod(0o755)
    return hashlib.sha256(contents.encode()).hexdigest()


def _fixtures(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    wrapper = shim_dir / "claude"
    inner = tmp_path / "real-claude"
    wrapper_digest = _executable(wrapper, '#!/bin/sh\nexec "$CAO_CONDUCTOR_REAL_CLAUDE" "$@"\n')
    inner_digest = _executable(inner, "#!/bin/sh\nexit 0\n")
    route_map = tmp_path / "routes.json"
    marker = tmp_path / "consumed"
    route_map.write_text(
        json.dumps(
            {
                "routes": {
                    str(worktree): {
                        "route": "glm",
                        "model": "glm-5.2[1m]",
                        "consumed_path": str(marker),
                    }
                }
            }
        )
    )
    envelope = {
        "wrapper_executable": str(wrapper),
        "wrapper_executable_sha256": wrapper_digest,
        "inner_executable": str(inner),
        "inner_executable_sha256": inner_digest,
        "route_map_path": str(route_map),
        "worktree_realpath": str(worktree),
        "consumed_marker_path": str(marker),
    }
    session = {
        "CAO_CONDUCTOR_ROUTES": str(route_map),
        "CAO_CONDUCTOR_SHIM_DIR": str(shim_dir),
        "CAO_CONDUCTOR_REAL_CLAUDE": str(inner),
        "CAO_CONDUCTOR_MODEL": "glm-5.2[1m]",
        "ANTHROPIC_API_KEY": "must-not-be-copied",
    }
    return worktree, inner, envelope, session


def test_session_map_binds_wrapper_inner_worktree_and_marker(tmp_path):
    worktree, inner, envelope, session = _fixtures(tmp_path)

    normalized = glm.validate_envelope(
        provider="claude_code",
        provider_route="glm",
        expected_model="glm-5.2[1m]",
        working_directory=str(worktree),
        provider_executable=str(inner),
        provider_executable_sha256=envelope["inner_executable_sha256"],
        envelope=envelope,
    )

    assert (
        glm.validate_session_env(
            session_env=session, envelope=normalized, expected_model="glm-5.2[1m]"
        )["CAO_CONDUCTOR_ROUTES"]
        == envelope["route_map_path"]
    )
    assert glm.consumed_marker_exists(envelope["consumed_marker_path"]) is False

    with pytest.raises(glm.GlmRouteError, match="session real Claude"):
        glm.validate_session_env(
            session_env={**session, "CAO_CONDUCTOR_REAL_CLAUDE": str(worktree)},
            envelope=normalized,
            expected_model="glm-5.2[1m]",
        )


def test_native_child_environment_uses_verified_session_map_only(tmp_path):
    worktree, inner, envelope, session = _fixtures(tmp_path)
    request = {
        "provider": "claude_code",
        "provider_route": "glm",
        "model": "glm-5.2[1m]",
        "route_envelope": envelope,
    }

    child_env = bridge.native_child_environment(request, session_env=session)

    assert child_env["CAO_CONDUCTOR_ROUTES"] == envelope["route_map_path"]
    assert child_env["PATH"].startswith(str(tmp_path / "shim") + ":")
    assert "ANTHROPIC_API_KEY" not in child_env
    assert child_env["CAO_CONDUCTOR_MODEL"] == "glm-5.2[1m]"
