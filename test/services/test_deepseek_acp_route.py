"""Closed DeepSeek ACP route-envelope validation (COND-0415).

The managed DeepSeek route is a wrapper/inner route envelope like the
native GLM route: only path names and digests cross the reservation
boundary, never credential bytes.  The one-shot token is a topology
fact (present before launch, consumed marker absent at validation
time), not a value anyone reads here.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from cli_agent_orchestrator.services import deepseek_acp_route


def _write_executable(path: Path, body: str) -> str:
    path.write_text(body)
    path.chmod(0o755)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _route_map(
    worktree: Path,
    *,
    model: str = "deepseek-v4-flash",
    token_path: Path | None = None,
    consumed_path: Path | None = None,
    route: str = "deepseek",
) -> Path:
    path = worktree / "deepseek-routes.json"
    path.write_text(
        json.dumps(
            {
                "schema": deepseek_acp_route.ROUTE_MAP_SCHEMA,
                "routes": {
                    str(worktree): {
                        "route": route,
                        "model": model,
                        "token_path": str(token_path or worktree / "deepseek-token.txt"),
                        "consumed_path": str(consumed_path or worktree / "deepseek-token.consumed"),
                    }
                },
            }
        )
    )
    return path


def _envelope(tmp_path: Path, **route_overrides: object) -> dict:
    """A complete, valid deepseek envelope plus its backing files."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    wrapper = tmp_path / "shims" / "claude"
    wrapper.parent.mkdir()
    wrapper_digest = _write_executable(
        wrapper, '#!/bin/sh\nexec "$CAO_CONDUCTOR_REAL_CLAUDE" "$@"\n'
    )
    inner = tmp_path / "real-claude"
    inner_digest = _write_executable(inner, "#!/bin/sh\necho 'claude 2.1.233'\n")
    token = tmp_path / "deepseek-token.txt"
    token.write_text("sk-one-shot\n")
    token.chmod(0o600)
    marker = tmp_path / "deepseek-token.consumed"
    route_map = _route_map(
        worktree,
        **{"token_path": token, "consumed_path": marker, **route_overrides},
    )
    return {
        "wrapper_executable": str(wrapper),
        "wrapper_executable_sha256": wrapper_digest,
        "inner_executable": str(inner),
        "inner_executable_sha256": inner_digest,
        "route_map_path": str(route_map),
        "worktree_realpath": str(worktree),
        "token_path": str(token),
        "consumed_marker_path": str(marker),
    }


def _validate(envelope: dict, *, provider: str = "claude_code", **overrides: object) -> dict:
    kwargs = {
        "provider": provider,
        "provider_route": deepseek_acp_route.PROVIDER_ROUTE_DEEPSEEK,
        "expected_model": "deepseek-v4-flash",
        "working_directory": envelope["worktree_realpath"],
        "provider_executable": envelope["inner_executable"],
        "provider_executable_sha256": envelope["inner_executable_sha256"],
        "envelope": envelope,
        "check_files": True,
    }
    kwargs.update(overrides)
    return deepseek_acp_route.validate_envelope(**kwargs)


class TestEnvelopeAcceptance:
    def test_accepts_exact_deepseek_topology(self, tmp_path: Path) -> None:
        envelope = _envelope(tmp_path)
        values = _validate(envelope)
        assert values["wrapper_executable"] == envelope["wrapper_executable"]
        assert values["inner_executable"] == envelope["inner_executable"]
        assert values["token_path"] == envelope["token_path"]
        assert values["consumed_marker_path"] == envelope["consumed_marker_path"]
        assert values["route_map_path"] == envelope["route_map_path"]

    def test_anthropic_route_returns_none_and_refuses_envelope(self, tmp_path: Path) -> None:
        envelope = _envelope(tmp_path)
        assert (
            deepseek_acp_route.validate_envelope(
                provider="claude_code",
                provider_route=deepseek_acp_route.PROVIDER_ROUTE_ANTHROPIC,
                expected_model="claude-3-7-sonnet-20250219",
                working_directory=envelope["worktree_realpath"],
                provider_executable=envelope["inner_executable"],
                provider_executable_sha256=envelope["inner_executable_sha256"],
                envelope=None,
                check_files=True,
            )
            is None
        )
        with pytest.raises(deepseek_acp_route.DeepSeekRouteError, match="valid only for"):
            deepseek_acp_route.validate_envelope(
                provider="claude_code",
                provider_route=deepseek_acp_route.PROVIDER_ROUTE_ANTHROPIC,
                expected_model="claude-3-7-sonnet-20250219",
                working_directory=envelope["worktree_realpath"],
                provider_executable=envelope["inner_executable"],
                provider_executable_sha256=envelope["inner_executable_sha256"],
                envelope=envelope,
                check_files=True,
            )

    def test_refuses_non_claude_provider(self, tmp_path: Path) -> None:
        envelope = _envelope(tmp_path)
        with pytest.raises(deepseek_acp_route.DeepSeekRouteError, match="provider=claude_code"):
            _validate(envelope, provider="codex")

    def test_refuses_unknown_route(self, tmp_path: Path) -> None:
        envelope = _envelope(tmp_path)
        with pytest.raises(deepseek_acp_route.DeepSeekRouteError, match="unknown provider_route"):
            _validate(envelope, provider_route="glm")


class TestEnvelopeIdentityPins:
    def test_inner_must_equal_reservation_executable(self, tmp_path: Path) -> None:
        envelope = _envelope(tmp_path)
        other = tmp_path / "other-claude"
        _write_executable(other, "#!/bin/sh\necho other\n")
        with pytest.raises(deepseek_acp_route.DeepSeekRouteError, match="inner_executable"):
            _validate(envelope, provider_executable=str(other))

    def test_inner_digest_must_equal_reservation_digest(self, tmp_path: Path) -> None:
        envelope = _envelope(tmp_path)
        with pytest.raises(deepseek_acp_route.DeepSeekRouteError, match="inner_executable_sha256"):
            _validate(envelope, provider_executable_sha256="0" * 64)

    def test_worktree_must_equal_working_directory(self, tmp_path: Path) -> None:
        envelope = _envelope(tmp_path)
        with pytest.raises(deepseek_acp_route.DeepSeekRouteError, match="worktree_realpath"):
            _validate(envelope, working_directory=str(tmp_path))

    def test_wrapper_digest_drift_is_refused(self, tmp_path: Path) -> None:
        envelope = _envelope(tmp_path)
        envelope["wrapper_executable_sha256"] = "0" * 64
        with pytest.raises(deepseek_acp_route.DeepSeekRouteError, match="wrapper_executable"):
            _validate(envelope)

    def test_non_canonical_paths_are_refused(self, tmp_path: Path) -> None:
        envelope = _envelope(tmp_path)
        envelope["token_path"] = str(tmp_path / ".." / tmp_path.name / "deepseek-token.txt")
        with pytest.raises(deepseek_acp_route.DeepSeekRouteError, match="token_path"):
            _validate(envelope)

    def test_missing_fields_are_refused(self, tmp_path: Path) -> None:
        envelope = _envelope(tmp_path)
        envelope.pop("token_path")
        with pytest.raises(deepseek_acp_route.DeepSeekRouteError, match="token_path"):
            _validate(envelope)


class TestTokenAndMarkerTopology:
    def test_token_missing_is_refused(self, tmp_path: Path) -> None:
        envelope = _envelope(tmp_path)
        Path(envelope["token_path"]).unlink()
        with pytest.raises(deepseek_acp_route.DeepSeekRouteError, match="token"):
            _validate(envelope)

    def test_marker_already_present_is_refused(self, tmp_path: Path) -> None:
        envelope = _envelope(tmp_path)
        Path(envelope["consumed_marker_path"]).write_text("consumed\n")
        with pytest.raises(deepseek_acp_route.DeepSeekRouteError, match="consumed marker"):
            _validate(envelope)

    def test_consumed_marker_content_must_be_exact(self, tmp_path: Path) -> None:
        marker = tmp_path / "marker.txt"
        marker.write_text("claimed\n")
        assert deepseek_acp_route.consumed_marker_exists(str(marker)) is False
        marker.write_text("consumed\n")
        assert deepseek_acp_route.consumed_marker_exists(str(marker)) is True

    def test_token_present_is_false_for_missing_or_symlink(self, tmp_path: Path) -> None:
        token = tmp_path / "token.txt"
        assert deepseek_acp_route.token_present(str(token)) is False
        target = tmp_path / "target.txt"
        target.write_text("x\n")
        token.symlink_to(target)
        assert deepseek_acp_route.token_present(str(token)) is False


class TestRouteMapCrossCheck:
    def test_route_map_missing_entry_is_refused(self, tmp_path: Path) -> None:
        envelope = _envelope(tmp_path)
        other = tmp_path / "other-worktree"
        other.mkdir()
        envelope["route_map_path"] = str(_route_map(other))
        with pytest.raises(deepseek_acp_route.DeepSeekRouteError, match="worktree"):
            _validate(envelope)

    def test_route_map_wrong_route_is_refused(self, tmp_path: Path) -> None:
        envelope = _envelope(tmp_path, route="anthropic")
        with pytest.raises(deepseek_acp_route.DeepSeekRouteError, match="deepseek route"):
            _validate(envelope)

    def test_route_map_wrong_model_is_refused(self, tmp_path: Path) -> None:
        envelope = _envelope(tmp_path, model="deepseek-v4-pro")
        with pytest.raises(deepseek_acp_route.DeepSeekRouteError, match="model"):
            _validate(envelope)

    def test_route_map_token_path_must_match_envelope(self, tmp_path: Path) -> None:
        other_token = tmp_path / "other-token.txt"
        other_token.write_text("x\n")
        envelope = _envelope(tmp_path, token_path=other_token)
        with pytest.raises(deepseek_acp_route.DeepSeekRouteError, match="token_path"):
            _validate(envelope)

    def test_route_map_consumed_path_must_match_envelope(self, tmp_path: Path) -> None:
        other_marker = tmp_path / "other-marker.txt"
        envelope = _envelope(tmp_path, consumed_path=other_marker)
        with pytest.raises(deepseek_acp_route.DeepSeekRouteError, match="consumed"):
            _validate(envelope)

    def test_unparseable_route_map_is_refused(self, tmp_path: Path) -> None:
        envelope = _envelope(tmp_path)
        Path(envelope["route_map_path"]).write_text("not json")
        with pytest.raises(deepseek_acp_route.DeepSeekRouteError, match="route map"):
            _validate(envelope)


class TestModelVocabulary:
    def test_allowlisted_models_round_trip(self) -> None:
        assert (
            deepseek_acp_route.validate_requested_model("deepseek-v4-flash") == "deepseek-v4-flash"
        )
        assert deepseek_acp_route.validate_requested_model("deepseek-v4-pro") == "deepseek-v4-pro"

    def test_other_models_are_refused(self) -> None:
        for model in ("deepseek-chat", "claude-3-7-sonnet-20250219", "", None):
            with pytest.raises(deepseek_acp_route.DeepSeekRouteError, match="not a pinnable"):
                deepseek_acp_route.validate_requested_model(model)

    def test_observed_model_matches_is_byte_exact(self) -> None:
        assert (
            deepseek_acp_route.observed_model_matches("deepseek-v4-flash", "deepseek-v4-flash")
            is True
        )
        assert deepseek_acp_route.observed_model_matches("deepseek-v4-flash", None) is False
        assert (
            deepseek_acp_route.observed_model_matches("deepseek-v4-flash", "deepseek-v4-pro")
            is False
        )
