"""Local API bearers must stay off remote assign, readiness and cleanup calls."""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.constants import API_BASE_URL
from cli_agent_orchestrator.utils import orchestration as orch

_TARGETS = [
    pytest.param("http://cao-worker-1:9889", None, id="remote"),
    pytest.param(API_BASE_URL, {"Authorization": "Bearer local-machine-token"}, id="local"),
]
_SRV = "cli_agent_orchestrator.utils.orchestration"


@pytest.mark.parametrize("base_url, expected_headers", _TARGETS)
@patch(f"{_SRV}.get_local_bearer", return_value="local-machine-token")
@patch(f"{_SRV}._own_delegation_ceiling", return_value=["*"])
@patch(f"{_SRV}._require_remote_delegation_ceiling", return_value=None)
@patch(f"{_SRV}.requests.post")
def test_assign_bearer_is_scoped_to_target(
    mock_post: MagicMock,
    _remote_ceiling: MagicMock,
    _own_ceiling: MagicMock,
    _bearer: MagicMock,
    base_url: str,
    expected_headers: dict[str, str] | None,
) -> None:
    mock_post.return_value.status_code = 200
    mock_post.return_value.json.return_value = {"id": "beef0002", "session_name": "cao-worker"}

    result = orch._assign_remote(
        agent_profile="developer",
        worker_message="Build it",
        current_terminal_id="a1b2c3d4",
        target_host=base_url,
        working_directory=None,
        engine=None,
        model=None,
        use_worktree=False,
        callback_url="http://cao-supervisor:9889",
    )

    assert result["success"] is True
    assert mock_post.call_count == 1
    assert mock_post.call_args.args[0] == f"{base_url}/sessions"
    assert mock_post.call_args.kwargs["headers"] == expected_headers


@pytest.mark.parametrize("base_url, expected_headers", _TARGETS)
@patch(f"{_SRV}.time.sleep")
@patch(f"{_SRV}.get_local_bearer", return_value="local-machine-token")
@patch(f"{_SRV}.requests.get")
def test_readiness_bearer_is_scoped_on_every_attempt(
    mock_get: MagicMock,
    _bearer: MagicMock,
    _sleep: MagicMock,
    base_url: str,
    expected_headers: dict[str, str] | None,
) -> None:
    mock_get.side_effect = [MagicMock(status_code=503), MagicMock(status_code=200)]

    orch._wait_remote_ready(base_url, timeout=5.0)

    assert mock_get.call_count == 2
    for call in mock_get.call_args_list:
        assert call.args[0] == f"{base_url}/health"
        assert call.kwargs["headers"] == expected_headers


@pytest.mark.parametrize("base_url, expected_headers", _TARGETS)
@patch(f"{_SRV}.get_local_bearer", return_value="local-machine-token")
@patch(f"{_SRV}.requests.delete")
def test_cleanup_bearer_is_scoped_to_target(
    mock_delete: MagicMock,
    _bearer: MagicMock,
    base_url: str,
    expected_headers: dict[str, str] | None,
) -> None:
    mock_delete.return_value.status_code = 200

    assert orch._cleanup_remote_terminal(base_url, "beef0002") is True
    assert mock_delete.call_count == 1
    assert mock_delete.call_args.args[0] == f"{base_url}/terminals/beef0002"
    assert mock_delete.call_args.kwargs["headers"] == expected_headers
