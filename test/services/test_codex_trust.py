from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from cli_agent_orchestrator.providers.codex import (
    CodexProvider,
    render_trusted_project_override,
)
from cli_agent_orchestrator.services import codex_trust, provider_contracts
from cli_agent_orchestrator.services.codex_trust import (
    CodexTrustProbeError,
    attest_trusted_project,
)


def test_route_attest_capability_is_the_canonical_provider_table():
    # The zero-task route attestor owns no single-version banner constant
    # of its own; it consumes the capability's canonical literal in
    # provider_contracts, exactly as the native-bind seam does. The Codex
    # cell admits 0.146.0 (preserved) and the stage-proven 0.147.0, and
    # nothing else.
    assert (
        codex_trust.ROUTE_ATTEST_CAPABLE_VERSIONS
        == provider_contracts.ROUTE_ATTEST_CAPABLE_VERSIONS[provider_contracts.PROVIDER_CODEX]
    )
    assert codex_trust.ROUTE_ATTEST_CAPABLE_VERSIONS == ("0.146.0", "0.147.0")


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


@pytest.mark.parametrize("banner", ["codex-cli 0.146.0\n", "codex-cli 0.147.0\n"])
def test_zero_turn_receipt_identity_is_exact_and_version_scoped(tmp_path, monkeypatch, banner):
    """The route-attest capability admits 0.147.0 with a byte-exact receipt.

    Both accepted builds run the identical zero-turn exchange — initialize,
    config/read, ephemeral thread/start, never a turn/start — so no prompt
    or task is ever admitted, the trust root resolves exactly ``trusted``
    from sessionFlags provenance, and the two receipts differ only in the
    ``codex_version`` key that records which build attested.
    """
    target = tmp_path / "fixture.with.dot" / ".worktrees" / "review"
    target.mkdir(parents=True)
    root = str(target.resolve())
    user_config = tmp_path / "config.toml"
    user_config.write_text('model = "placeholder"\n')
    app_requests: list = []

    def fake_run(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout=banner, stderr="")

    def fake_app_server(argv, requests, timeout):
        app_requests.append(requests)
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

    assert receipt["codex_version"] == banner.strip()
    assert receipt["no_turn_started"] is True
    # The zero-turn trust/route identity stays exact for both accepted builds.
    assert receipt["project_root"] == root
    assert receipt["trust_level"] == "trusted"
    assert receipt["config_origin"] == "sessionFlags"
    assert receipt["model"] == "gpt-5.6-sol"
    assert receipt["model_provider"] == "openai"
    assert receipt["reasoning_effort"] == "xhigh"
    assert receipt["cwd"] == root
    assert receipt["thread_id"] == "thread-zero-turn"
    assert receipt["probe_version"] == codex_trust.PROBE_VERSION
    assert len(receipt["argv_sha256"]) == 64
    # The same zero-task request sequence, never a turn/start.
    assert [item.get("method") for item in app_requests[0]] == [
        "initialize",
        "initialized",
        "config/read",
        "thread/start",
    ]
    assert all(item.get("method") != "turn/start" for item in app_requests[0])


def test_probe_fails_closed_on_codex_0148(tmp_path, monkeypatch):
    """Admitting 0.147.0 for zero-task route attestation is not a range.

    A 0.148.0 install stays fail-closed: its app-server mechanics have not
    been proven for this capability, so the breaker stays un-rearmed until
    that build is stage-verified.
    """
    target = tmp_path / "worktree"
    target.mkdir()
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="codex-cli 0.148.0\n", stderr=""
        ),
    )
    with pytest.raises(
        CodexTrustProbeError, match=r"unsupported Codex version 'codex-cli 0\.148\.0'"
    ):
        attest_trusted_project(
            str(target.resolve()),
            expected_model="gpt-5.6-sol",
            expected_effort="xhigh",
            user_config_path=tmp_path / "absent.toml",
        )


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
    # The previously pinned banner (0.145.0) must fail closed once the
    # capability has moved: admission is exact-set membership — 0.146.0
    # and 0.147.0 for the zero-task route-attestation capability — never a
    # range or a minimum.
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
        match=r"unsupported Codex version 'codex-cli 0\.145\.0'; expected one of \['0\.146\.0', '0\.147\.0'\]",
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
    # The unified composer emits the route last (--model then the
    # reasoning-effort override), after the canonical trust override.
    assert command.endswith("--model gpt-5.6-sol -c 'model_reasoning_effort=\"xhigh\"'")
