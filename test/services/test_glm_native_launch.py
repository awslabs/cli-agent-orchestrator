"""Closed-route proofs for the native GLM reservation envelope."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

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


def test_validate_envelope_rejects_wrong_route_or_provider(tmp_path):
    worktree, inner, envelope, _ = _fixtures(tmp_path)
    common = {
        "expected_model": "glm-5.2[1m]",
        "working_directory": str(worktree),
        "provider_executable": str(inner),
        "provider_executable_sha256": envelope["inner_executable_sha256"],
        "envelope": envelope,
    }

    with pytest.raises(glm.GlmRouteError, match="unknown provider_route"):
        glm.validate_envelope(provider="claude_code", provider_route="other", **common)
    with pytest.raises(glm.GlmRouteError, match="supported only for provider=claude_code"):
        glm.validate_envelope(provider="kimi_cli", provider_route="glm", **common)


def test_validate_envelope_rejects_a_missing_envelope(tmp_path):
    worktree, inner, envelope, _ = _fixtures(tmp_path)

    with pytest.raises(glm.GlmRouteError, match="requires a route_envelope"):
        glm.validate_envelope(
            provider="claude_code",
            provider_route="glm",
            expected_model="glm-5.2[1m]",
            working_directory=str(worktree),
            provider_executable=str(inner),
            provider_executable_sha256=envelope["inner_executable_sha256"],
            envelope=None,
        )


def test_validate_envelope_rejects_worktree_and_inner_digest_drift(tmp_path):
    worktree, inner, envelope, _ = _fixtures(tmp_path)
    other_worktree = tmp_path / "other-worktree"
    other_worktree.mkdir()

    with pytest.raises(glm.GlmRouteError, match="worktree_realpath"):
        glm.validate_envelope(
            provider="claude_code",
            provider_route="glm",
            expected_model="glm-5.2[1m]",
            working_directory=str(other_worktree),
            provider_executable=str(inner),
            provider_executable_sha256=envelope["inner_executable_sha256"],
            envelope=envelope,
        )
    with pytest.raises(glm.GlmRouteError, match="inner_executable_sha256"):
        glm.validate_envelope(
            provider="claude_code",
            provider_route="glm",
            expected_model="glm-5.2[1m]",
            working_directory=str(worktree),
            provider_executable=str(inner),
            provider_executable_sha256="0" * 64,
            envelope=envelope,
        )


def test_validate_envelope_rejects_a_route_map_mismatch(tmp_path):
    worktree, inner, envelope, _ = _fixtures(tmp_path)
    Path(envelope["route_map_path"]).write_text(
        json.dumps(
            {
                "routes": {
                    str(worktree): {
                        "route": "anthropic",
                        "model": "glm-5.2[1m]",
                        "consumed_path": envelope["consumed_marker_path"],
                    }
                }
            }
        )
    )

    with pytest.raises(glm.GlmRouteError, match="route map entry is not a GLM route"):
        glm.validate_envelope(
            provider="claude_code",
            provider_route="glm",
            expected_model="glm-5.2[1m]",
            working_directory=str(worktree),
            provider_executable=str(inner),
            provider_executable_sha256=envelope["inner_executable_sha256"],
            envelope=envelope,
        )


def test_validate_session_env_rejects_missing_or_mismatched_stored_env(tmp_path):
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

    with pytest.raises(glm.GlmRouteError, match="requires a stored session environment"):
        glm.validate_session_env(session_env={}, envelope=normalized, expected_model="glm-5.2[1m]")
    with pytest.raises(glm.GlmRouteError, match="stored session route map differs"):
        glm.validate_session_env(
            session_env={**session, "CAO_CONDUCTOR_ROUTES": str(tmp_path / "other.json")},
            envelope=normalized,
            expected_model="glm-5.2[1m]",
        )


@pytest.mark.parametrize(
    ("field", "expected_message"),
    [
        ("model", "stored session route entry does not match"),
        ("marker", "stored session consumed marker differs"),
    ],
)
def test_validate_session_env_rejects_route_model_or_marker_mismatch(
    tmp_path, field, expected_message
):
    worktree, inner, envelope, session = _fixtures(tmp_path)
    route_map = Path(envelope["route_map_path"])
    route_data = json.loads(route_map.read_text())
    route_entry = route_data["routes"][str(worktree)]
    if field == "model":
        route_entry["model"] = "glm-5.2"
    else:
        route_entry["consumed_path"] = str(tmp_path / "different-marker")
    route_map.write_text(json.dumps(route_data))
    normalized = glm.validate_envelope(
        provider="claude_code",
        provider_route="glm",
        expected_model="glm-5.2[1m]",
        working_directory=str(worktree),
        provider_executable=str(inner),
        provider_executable_sha256=envelope["inner_executable_sha256"],
        envelope=envelope,
        check_files=False,
    )

    with pytest.raises(glm.GlmRouteError, match=expected_message):
        glm.validate_session_env(
            session_env=session, envelope=normalized, expected_model="glm-5.2[1m]"
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
