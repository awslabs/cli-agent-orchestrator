"""``cao launch`` sends allowed_tools only when the user set it (haofeif #9, #802).

The shared-server ``/sessions`` branch used to serialize the client-resolved
tool policy unconditionally, overriding the server installation's own profile
policy with whatever the CLIENT's profile store (or its broad missing-profile
default) resolved. It must send ``allowed_tools`` only when the user passed
``--allowed-tools`` or ``--yolo``.
"""

from unittest.mock import patch

from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.launch import launch


def _run(args):
    runner = CliRunner()
    with patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as mock_post:
        mock_post.return_value.json.return_value = {
            "session_name": "s",
            "id": "term1234",
            "name": "t",
        }
        mock_post.return_value.raise_for_status.return_value = None
        result = runner.invoke(launch, args)
    return result, mock_post


def _sessions_params(mock_post):
    for call in mock_post.call_args_list:
        url = call.args[0] if call.args else ""
        if url.endswith("/sessions"):
            return call.kwargs.get("params", {})
    raise AssertionError("no POST /sessions call was made")


def test_no_flag_omits_allowed_tools():
    """Without --allowed-tools/--yolo the param is absent — server resolves policy."""
    result, mock_post = _run(["--agents", "some-agent", "--headless", "--auto-approve"])
    assert result.exit_code == 0, result.output
    assert "allowed_tools" not in _sessions_params(mock_post)


def test_explicit_allowed_tools_is_sent():
    result, mock_post = _run(
        ["--agents", "some-agent", "--headless", "--auto-approve", "--allowed-tools", "fs_read"]
    )
    assert result.exit_code == 0, result.output
    assert _sessions_params(mock_post).get("allowed_tools") == "fs_read"


def test_yolo_sends_allowed_tools():
    result, mock_post = _run(["--agents", "some-agent", "--headless", "--yolo"])
    assert result.exit_code == 0, result.output
    assert _sessions_params(mock_post).get("allowed_tools") == "*"
