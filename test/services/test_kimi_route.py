from __future__ import annotations

import os
import subprocess
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
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="0.30.1\n", stderr=""),
    )
    with pytest.raises(KimiRouteProbeError, match="unsupported Kimi version"):
        attest_kimi_route(
            str(tmp_path.resolve()),
            expected_model="kimi-code/k3",
            expected_effort="max",
            user_config_path=tmp_path / "absent.toml",
        )


@pytest.mark.parametrize("banner", ["not-a-version\n", "\n"])
def test_probe_fails_closed_on_a_malformed_version(tmp_path, monkeypatch, banner):
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=banner, stderr=""),
    )
    with pytest.raises(KimiRouteProbeError, match="unsupported Kimi version"):
        attest_kimi_route(
            str(tmp_path.resolve()),
            expected_model="kimi-code/k3",
            expected_effort="max",
            user_config_path=tmp_path / "absent.toml",
        )


def test_probe_fails_closed_on_a_nonzero_version_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    with pytest.raises(KimiRouteProbeError, match="unsupported Kimi version"):
        attest_kimi_route(
            str(tmp_path.resolve()),
            expected_model="kimi-code/k3",
            expected_effort="max",
            user_config_path=tmp_path / "absent.toml",
        )


def test_probe_fails_closed_when_the_binary_cannot_start(tmp_path, monkeypatch):
    def fake_run(*args, **kwargs):
        raise OSError("No such file or directory")

    monkeypatch.setattr("subprocess.run", fake_run)
    with pytest.raises(KimiRouteProbeError, match="could not execute Kimi version probe"):
        attest_kimi_route(
            str(tmp_path.resolve()),
            expected_model="kimi-code/k3",
            expected_effort="max",
            user_config_path=tmp_path / "absent.toml",
        )


def test_a_slow_but_valid_version_answer_is_admitted_within_the_provider_bound(
    tmp_path, monkeypatch
):
    """COND-0313: a healthy but slow Kimi ``--version`` must not fail the route.

    The live failure: the pinned 0.31.0 executable answered in 0.37–0.41 s
    warm, yet one campaign launch observed it miss a fixed 5 s deadline
    under startup load and the launch failed closed before any delivery.
    The contract is one bounded, provider-appropriate observation — 20 s,
    the same bound the native-TUI acceptance harness already allows for
    this exact probe — never a replayed launch.
    """
    observed = {}

    def fake_run(argv, **kwargs):
        observed.update(kwargs)
        if kwargs.get("timeout", 0) < 12.0:
            # A provider that needs 12 s to answer: any tighter deadline
            # times out here, the provider-appropriate bound does not.
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return SimpleNamespace(returncode=0, stdout="0.29.0\n", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("cli_agent_orchestrator.services.kimi_route._AcpClient", _FakeAcpClient)
    receipt = attest_kimi_route(
        str(tmp_path.resolve()),
        expected_model="kimi-code/k3",
        expected_effort="max",
        user_config_path=tmp_path / "absent.toml",
    )

    assert receipt["kimi_version"] == "0.29.0"
    assert observed["timeout"] == 20.0


def test_a_version_probe_beyond_the_provider_bound_fails_closed(tmp_path, monkeypatch):
    """The deadline stays finite, and the error names the command and it."""

    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr("subprocess.run", fake_run)
    with pytest.raises(KimiRouteProbeError) as excinfo:
        attest_kimi_route(
            str(tmp_path.resolve()),
            expected_model="kimi-code/k3",
            expected_effort="max",
            user_config_path=tmp_path / "absent.toml",
        )
    message = str(excinfo.value)
    assert "could not execute Kimi version probe" in message
    assert "'--version'" in message
    assert "timed out after 20.0 seconds" in message


def test_the_probe_suppresses_the_updater_for_both_kimi_processes(tmp_path, monkeypatch):
    """COND-0315: the attestor's ``--version`` and ``acp`` children both run
    under the deterministic kill-switch, and an ambient conflicting value
    cannot re-enable the updater for a CAO-managed attestation."""
    observed = {}
    clients = []

    class _FakeAcp0330(_FakeAcpClient):
        def request(self, method, params):
            if method == "initialize":
                return {
                    "protocolVersion": 1,
                    "agentInfo": {"name": "Kimi Code CLI", "version": "0.33.0"},
                }
            return super().request(method, params)

    def fake_run(argv, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="0.33.0\n", stderr="")

    def fake_client(argv, env, timeout):
        client = _FakeAcp0330(argv, env, timeout)
        clients.append(client)
        return client

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("cli_agent_orchestrator.services.kimi_route._AcpClient", fake_client)
    monkeypatch.setattr(os, "environ", {"KIMI_CODE_NO_AUTO_UPDATE": "0", "HOME": "/home/test"})
    receipt = attest_kimi_route(
        str(tmp_path.resolve()),
        expected_model="kimi-code/k3",
        expected_effort="max",
        user_config_path=tmp_path / "absent.toml",
    )

    assert receipt["kimi_version"] == "0.33.0"
    # The bounded --version probe observes under suppression...
    assert observed["env"]["KIMI_CODE_NO_AUTO_UPDATE"] == "1"
    # ...and so does the ACP client, regardless of the ambient conflict.
    assert clients[0].env["KIMI_CODE_NO_AUTO_UPDATE"] == "1"


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
