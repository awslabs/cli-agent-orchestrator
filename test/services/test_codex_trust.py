from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from cli_agent_orchestrator.providers.codex import (
    CodexProvider,
    render_trusted_project_override,
)
from cli_agent_orchestrator.services import codex_trust
from cli_agent_orchestrator.services.codex_trust import (
    CodexTrustProbeError,
    attest_trusted_project,
)


def test_supported_codex_version_is_exactly_0146_0():
    # The fail-closed gate pins exactly one codex-cli banner — currently
    # 0.146.0 — never a range, minimum, or wildcard. Move the constant and
    # this test together, and only after a version-specific app-server
    # protocol audit of the methods the bridge and trust probe use.
    assert codex_trust.SUPPORTED_CODEX_VERSION == "codex-cli 0.146.0"


def _app_server_stdout(root: str) -> str:
    responses = [
        {"id": 1, "result": {"serverInfo": {"name": "codex"}}},
        {
            "id": 2,
            "result": {
                "config": {"projects": {root: {"trust_level": "trusted"}}},
                "origins": {"projects": {root: {"trust_level": "sessionFlags"}}},
                "layers": [],
            },
        },
        {
            "id": 3,
            "result": {
                "cwd": root,
                "model": "gpt-5.6-sol",
                "modelProvider": "openai",
                "reasoningEffort": "xhigh",
                "thread": {"id": "thread-zero-turn"},
            },
        },
    ]
    return "".join(json.dumps(item) + "\n" for item in responses)


def test_trust_override_renders_dotted_worktree_key_byte_exact(tmp_path):
    target = tmp_path / "fixture.with.dot" / ".worktrees" / "review"
    target.mkdir(parents=True)
    root = str(target.resolve())
    assert render_trusted_project_override(root) == (
        f'projects={{"{root}"={{trust_level="trusted"}}}}'
    )


def test_trust_override_rejects_noncanonical_or_relative_path(tmp_path):
    target = tmp_path / "worktree"
    target.mkdir()
    with pytest.raises(ValueError):
        render_trusted_project_override("relative/path")
    with pytest.raises(ValueError):
        render_trusted_project_override(str(target / ".." / "worktree"))


def test_probe_verifies_config_origin_route_and_zero_turn(tmp_path, monkeypatch):
    target = tmp_path / "fixture.with.dot" / ".worktrees" / "review"
    target.mkdir(parents=True)
    root = str(target.resolve())
    user_config = tmp_path / "config.toml"
    user_config.write_text('model = "placeholder"\n')
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="codex-cli 0.146.0\n", stderr="")

    def fake_app_server(argv, requests, timeout):
        calls.append((argv, {"requests": requests, "timeout": timeout}))
        return _app_server_stdout(root), "", -15

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.codex_trust._run_app_server_probe",
        fake_app_server,
    )
    receipt = attest_trusted_project(
        root,
        expected_model="gpt-5.6-sol",
        expected_effort="xhigh",
        user_config_path=user_config,
    )

    assert receipt["project_root"] == root
    assert receipt["config_origin"] == "sessionFlags"
    assert receipt["model"] == "gpt-5.6-sol"
    assert receipt["reasoning_effort"] == "xhigh"
    assert receipt["no_turn_started"] is True
    app_argv, app_kwargs = calls[1]
    assert render_trusted_project_override(root) in app_argv
    requests = app_kwargs["requests"]
    assert [item.get("method") for item in requests] == [
        "initialize",
        "initialized",
        "config/read",
        "thread/start",
    ]
    assert all(item.get("method") != "turn/start" for item in requests)


def test_probe_fails_closed_on_version_drift(tmp_path, monkeypatch):
    target = tmp_path / "worktree"
    target.mkdir()
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="codex-cli 0.146.1\n", stderr=""
        ),
    )
    with pytest.raises(CodexTrustProbeError, match="unsupported Codex version"):
        attest_trusted_project(
            str(target.resolve()),
            expected_model="gpt-5.6-sol",
            expected_effort="xhigh",
            user_config_path=tmp_path / "absent.toml",
        )


def test_probe_fails_closed_on_previous_pin_0145_0(tmp_path, monkeypatch):
    # The previously pinned banner (0.145.0) must fail closed once the pin
    # has moved: the gate is an exact-version check, never a range or a
    # minimum.
    target = tmp_path / "worktree"
    target.mkdir()
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="codex-cli 0.145.0\n", stderr=""
        ),
    )
    with pytest.raises(
        CodexTrustProbeError,
        match=r"unsupported Codex version 'codex-cli 0\.145\.0'; expected 'codex-cli 0\.146\.0'",
    ):
        attest_trusted_project(
            str(target.resolve()),
            expected_model="gpt-5.6-sol",
            expected_effort="xhigh",
            user_config_path=tmp_path / "absent.toml",
        )


def test_codex_command_carries_typed_trust_override(tmp_path, monkeypatch):
    target = tmp_path / ".worktrees" / "review"
    target.mkdir(parents=True)
    root = str(target.resolve())
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.codex.load_agent_profile",
        lambda _name: SimpleNamespace(
            codexProfile=None,
            model="gpt-5.6-sol",
            system_prompt="",
            mcpServers=None,
            codexConfig={"model_reasoning_effort": "xhigh"},
        ),
    )
    provider = CodexProvider(
        "deadbeef",
        "cao-test",
        "reviewer",
        "reviewer-sol-max",
        ["read"],
        trusted_project_root=root,
        expected_model="gpt-5.6-sol",
        expected_effort="xhigh",
    )
    command = provider._build_codex_command()
    assert render_trusted_project_override(root) in command
    assert command.endswith("-c 'model=\"gpt-5.6-sol\"' -c 'model_reasoning_effort=\"xhigh\"'")
