"""Tests for cross-node deferred-init failure notification (MF1).

A worker created remotely (assign with ``target_host``) has no local
``caller_id`` row; the supervisor injected CAO_CALLBACK_URL /
CAO_CALLBACK_TERMINAL_ID into the session env at creation. When deferred init
fails, ``_notify_caller_of_deferred_failure`` must POST the failure to the
supervisor node's inbox instead of dropping it into a log nobody reads —
otherwise assign returned success=True and the promised callback never comes.
"""

import os
from unittest.mock import MagicMock, patch

from cli_agent_orchestrator.services.terminal_service import _notify_caller_of_deferred_failure

_TS = "cli_agent_orchestrator.services.terminal_service"

_CALLBACK_ENV = {
    "CAO_CALLBACK_URL": "http://cao-supervisor:9889",
    "CAO_CALLBACK_TERMINAL_ID": "a1b2c3d4",
}


def _ok_response():
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    return resp


class TestCrossNodeDeferredFailureNotify:
    @patch(f"{_TS}.create_inbox_message")
    @patch(f"{_TS}.requests")
    @patch(f"{_TS}.get_session_env", return_value=dict(_CALLBACK_ENV))
    @patch(
        f"{_TS}.get_terminal_metadata",
        return_value={"caller_id": None, "tmux_session": "cao-remote"},
    )
    def test_no_local_caller_posts_to_cross_node_supervisor(
        self, mock_meta, mock_env, mock_requests, mock_inbox
    ):
        mock_requests.post.return_value = _ok_response()

        _notify_caller_of_deferred_failure(
            "feed0001", "Worker feed0001 failed to initialize", registry=None, delete_worker=False
        )

        args, kwargs = mock_requests.post.call_args
        assert args[0] == "http://cao-supervisor:9889/terminals/a1b2c3d4/inbox/messages"
        assert kwargs["params"] == {
            "sender_id": "feed0001",
            "message": "Worker feed0001 failed to initialize",
        }
        mock_env.assert_called_once_with("cao-remote")
        # The local inbox path must not be attempted (no local caller row).
        mock_inbox.assert_not_called()

    @patch(f"{_TS}.create_inbox_message")
    @patch(f"{_TS}.requests")
    @patch(
        f"{_TS}.get_session_env",
        return_value={
            "CAO_CALLBACK_URL": "http://cao-worker-broker:9890",
            "CAO_CALLBACK_TERMINAL_ID": "a1b2c3d4",
        },
    )
    @patch(
        f"{_TS}.get_terminal_metadata",
        return_value={"caller_id": None, "tmux_session": "cao-remote"},
    )
    def test_elastic_failure_callback_carries_gateway_credentials(
        self, mock_meta, mock_env, mock_requests, mock_inbox
    ):
        mock_requests.post.return_value = _ok_response()
        with patch.dict(
            os.environ,
            {
                "CAO_ELASTIC_WORKER_ID": "deadbeef",
                "CAO_ELASTIC_RELEASE_TOKEN": "release-token",
            },
            clear=False,
        ):
            _notify_caller_of_deferred_failure(
                "feed0001", "provider failed", registry=None, delete_worker=False
            )

        assert mock_requests.post.call_args.kwargs["headers"] == {
            "X-CAO-Worker-ID": "deadbeef",
            "X-CAO-Release-Token": "release-token",
        }
        mock_inbox.assert_not_called()

    @patch(f"{_TS}.requests")
    @patch(f"{_TS}.create_inbox_message")
    @patch(
        f"{_TS}.get_terminal_metadata",
        return_value={"caller_id": "0badcafe", "tmux_session": "cao-local"},
    )
    def test_local_caller_path_unchanged(self, mock_meta, mock_inbox, mock_requests):
        _notify_caller_of_deferred_failure("feed0002", "boom", registry=None, delete_worker=False)
        mock_inbox.assert_called_once_with(
            sender_id="feed0002", receiver_id="0badcafe", message="boom"
        )
        mock_requests.post.assert_not_called()

    @patch(f"{_TS}.requests")
    @patch(f"{_TS}.create_inbox_message")
    @patch(f"{_TS}.get_session_env", return_value={})
    @patch(
        f"{_TS}.get_terminal_metadata",
        return_value={"caller_id": None, "tmux_session": "cao-orphan"},
    )
    def test_no_caller_and_no_callback_env_is_log_only(
        self, mock_meta, mock_env, mock_inbox, mock_requests
    ):
        _notify_caller_of_deferred_failure("feed0003", "boom", registry=None, delete_worker=False)
        mock_inbox.assert_not_called()
        mock_requests.post.assert_not_called()

    @patch(f"{_TS}.delete_terminal")
    @patch(f"{_TS}.requests")
    @patch(f"{_TS}.get_session_env", return_value=dict(_CALLBACK_ENV))
    @patch(
        f"{_TS}.get_terminal_metadata",
        return_value={"caller_id": None, "tmux_session": "cao-remote"},
    )
    def test_notify_failure_never_blocks_teardown(
        self, mock_meta, mock_env, mock_requests, mock_delete
    ):
        """Best-effort contract: even if the cross-node POST blows up, the
        dead worker must still be torn down (it occupies a max=1 pod's slot)."""
        mock_requests.post.side_effect = Exception("supervisor unreachable")

        _notify_caller_of_deferred_failure("feed0004", "boom", registry=None, delete_worker=True)

        mock_delete.assert_called_once_with("feed0004", registry=None)
