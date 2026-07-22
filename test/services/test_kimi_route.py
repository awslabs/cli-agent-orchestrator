from __future__ import annotations

from types import SimpleNamespace

import pytest

from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider
from cli_agent_orchestrator.services.kimi_route import (
    KimiRouteProbeError,
    attest_kimi_route,
)


class _FakeAcpClient:
    def __init__(self, argv, env, timeout):
        self.argv = argv
        self.env = env
        self.timeout = timeout
        self.calls = []

    def request(self, method, params):
        self.calls.append((method, params))
        if method == "initialize":
            return {
                "protocolVersion": 1,
                "agentInfo": {"name": "Kimi Code CLI", "version": "0.29.0"},
            }
        if method == "session/new":
            return {
                "sessionId": "session-zero-prompt",
                "configOptions": [
                    {
                        "id": "model",
                        "category": "model",
                        "currentValue": "kimi-code/k3",
                    },
                    {
                        "id": "thinking",
                        "category": "thought_level",
                        "currentValue": "max",
                    },
                ],
            }
        raise AssertionError(f"unexpected ACP method: {method}")

    def close(self):
        return -15, ""


def test_probe_attests_k3_max_without_prompt(tmp_path, monkeypatch):
    root = str(tmp_path.resolve())
    config = tmp_path / "config.toml"
    config.write_text('default_model = "kimi-code/k3"\n')
    clients = []

    def fake_client(argv, env, timeout):
        client = _FakeAcpClient(argv, env, timeout)
        clients.append(client)
        return client

    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="0.29.0\n", stderr=""),
    )
    monkeypatch.setattr("cli_agent_orchestrator.services.kimi_route._AcpClient", fake_client)
    receipt = attest_kimi_route(
        root,
        expected_model="kimi-code/k3",
        expected_effort="max",
        user_config_path=config,
    )

    assert receipt["model"] == "kimi-code/k3"
    assert receipt["reasoning_effort"] == "max"
    assert receipt["no_prompt_sent"] is True
    assert receipt["terminal_model_argv"] == ["--model", "kimi-code/k3"]
    assert receipt["terminal_effort_env"] == {"KIMI_MODEL_THINKING_EFFORT": "max"}
    assert [method for method, _ in clients[0].calls] == ["initialize", "session/new"]
    assert clients[0].env["KIMI_MODEL_THINKING_EFFORT"] == "max"


def test_probe_fails_closed_on_version_drift(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="0.30.0\n", stderr=""),
    )
    with pytest.raises(KimiRouteProbeError, match="unsupported Kimi version"):
        attest_kimi_route(
            str(tmp_path.resolve()),
            expected_model="kimi-code/k3",
            expected_effort="max",
            user_config_path=tmp_path / "absent.toml",
        )


def test_managed_kimi_command_forces_attested_route_last():
    provider = KimiCliProvider(
        "deadbeef",
        "cao-test",
        "worker",
        expected_model="kimi-code/k3",
        expected_effort="max",
    )
    command = provider._build_kimi_command()
    try:
        assert "KIMI_MODEL_THINKING_EFFORT=max" in command
        assert command.endswith("kimi --yolo --model kimi-code/k3")
    finally:
        provider.cleanup()
