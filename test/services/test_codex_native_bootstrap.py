"""The Codex app-server bootstrap mints a persistent thread without a turn."""

import hashlib
import json
import os

import pytest

from cli_agent_orchestrator.services import codex_native_bootstrap as cnb
from cli_agent_orchestrator.services import native_attachment, provider_contracts

SESSION = "019fb17d-0c6d-7161-a408-6b1fa61c8f2d"


@pytest.fixture
def codex_binary(tmp_path):
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    path = os.path.realpath(binary)
    return path, hashlib.sha256(binary.read_bytes()).hexdigest()


def _response(request_id, result):
    return json.dumps({"id": request_id, "result": result})


def test_zero_turn_bootstrap_capability_is_narrower_than_provider_capability():
    """0.147 has the bootstrap proof without changing the broad version table."""
    assert cnb.BOOTSTRAP_CAPABLE_VERSIONS == ("0.146.0", "0.147.0")
    # One literal, two consumers: the bootstrap that mints the native id and
    # the managed bind seam that accepts it read the same table object, so
    # the two surfaces cannot drift back into disagreement — which is the
    # reproduced failure where mint accepted 0.147.0 and bind refused it.
    assert (
        cnb.BOOTSTRAP_CAPABLE_VERSIONS
        is provider_contracts.NATIVE_BIND_CAPABLE_VERSIONS[provider_contracts.PROVIDER_CODEX]
    )
    assert cnb.is_bootstrap_capable_build("codex-cli 0.147.0") is True
    assert cnb.is_bootstrap_capable_build("codex-cli 0.148.0") is False
    assert (
        provider_contracts.is_listed_version(provider_contracts.PROVIDER_CODEX, "codex-cli 0.147.0")
        is False
    )


def test_bootstrap_materializes_a_resumable_zero_turn_thread(tmp_path, codex_binary, monkeypatch):
    seen = {}
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()

    def exchange(argv, requests, timeout, *, env=None, followup_factory=None):
        seen["argv"] = argv
        seen["requests"] = requests
        seen["env"] = env
        start_response = {
            "id": 3,
            "result": {
                "thread": {"id": SESSION},
                "model": "gpt-5.6-sol",
                "reasoningEffort": "xhigh",
                "cwd": os.path.realpath(tmp_path),
            },
        }
        followups = followup_factory({3: start_response})
        seen["followups"] = followups
        rollout = codex_home / "sessions" / "2026" / "07" / "30"
        rollout.mkdir(parents=True)
        (rollout / f"rollout-test-{SESSION}.jsonl").write_text('{"type":"session_meta"}\n')
        stdout = "\n".join(
            [
                _response(1, {"userAgent": "codex-test"}),
                _response(
                    2,
                    {
                        "config": {
                            "projects": {os.path.realpath(tmp_path): {"trust_level": "trusted"}}
                        }
                    },
                ),
                json.dumps(start_response),
                _response(4, {}),
            ]
        )
        return stdout, "", -15

    monkeypatch.setattr(cnb, "_run_app_server_probe", exchange)
    path, digest = codex_binary
    receipt = cnb.mint_session(
        codex_binary=path,
        binary_sha256=digest,
        version_output="codex-cli 0.146.0",
        working_directory=os.path.realpath(tmp_path),
        model="gpt-5.6-sol",
        effort="xhigh",
        profile_args=["--yolo"],
        environment={"PATH": os.environ["PATH"], "CODEX_HOME": str(codex_home)},
    )

    methods = [request["method"] for request in seen["requests"]]
    assert methods == ["initialize", "initialized", "config/read", "thread/start"]
    assert seen["followups"] == [
        {
            "id": 4,
            "method": "thread/name/set",
            "params": {
                "threadId": SESSION,
                "name": f"CAO managed Codex session {SESSION[:8]}",
            },
        }
    ]
    thread_params = seen["requests"][-1]["params"]
    assert thread_params["ephemeral"] is False
    assert "turn/start" not in methods
    assert receipt["native_session_id"] == SESSION
    assert receipt["sent_no_turn"] is True
    assert receipt["materialization_method"] == "thread/name/set"
    assert receipt["materialization_sent_no_turn"] is True
    assert receipt["rollout_path"].endswith(f"-{SESSION}.jsonl")
    assert receipt["detached_before_launch"] is True
    assert receipt["exit_proof"]["reaped"] is True
    assert seen["argv"][-2:] == ["app-server", "--stdio"]

    intent = cnb.bootstrap_intent(receipt)
    assert intent["acquisition_method"] == native_attachment.ACQUISITION_ZERO_TURN_BOOTSTRAP


def test_default_route_omits_model_and_records_provider_observation(
    tmp_path, codex_binary, monkeypatch
):
    seen = {}
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()

    def exchange(argv, requests, _timeout, *, env=None, followup_factory=None):
        seen["argv"] = argv
        seen["requests"] = requests
        start_response = {
            "id": 3,
            "result": {
                "thread": {"id": SESSION},
                "model": "gpt-5.6-sol",
                "reasoningEffort": None,
                "cwd": os.path.realpath(tmp_path),
            },
        }
        followup_factory({3: start_response})
        rollout = codex_home / "sessions" / "2026" / "08" / "09"
        rollout.mkdir(parents=True)
        (rollout / f"rollout-test-{SESSION}.jsonl").write_text('{"type":"session_meta"}\n')
        return (
            "\n".join(
                [
                    _response(1, {"userAgent": "codex-test"}),
                    _response(
                        2,
                        {
                            "config": {
                                "projects": {os.path.realpath(tmp_path): {"trust_level": "trusted"}}
                            }
                        },
                    ),
                    json.dumps(start_response),
                    _response(4, {}),
                ]
            ),
            "",
            0,
        )

    monkeypatch.setattr(cnb, "_run_app_server_probe", exchange)
    path, digest = codex_binary
    profile_args = [
        "--yolo",
        "-c",
        'projects={"' + os.path.realpath(tmp_path) + '":{trust_level="trusted"}}',
    ]
    receipt = cnb.mint_session(
        codex_binary=path,
        binary_sha256=digest,
        version_output="codex-cli 0.147.0",
        working_directory=os.path.realpath(tmp_path),
        model=None,
        effort=None,
        profile_args=profile_args,
        environment={"CODEX_HOME": str(codex_home)},
    )

    assert seen["argv"] == [path, *profile_args, "app-server", "--stdio"]
    assert seen["requests"][-1] == {
        "id": 3,
        "method": "thread/start",
        "params": {
            "cwd": os.path.realpath(tmp_path),
            "ephemeral": False,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
        },
    }
    assert receipt["model"] == "gpt-5.6-sol"
    assert receipt["effort"] is None
    assert receipt["requested_model"] is None
    assert receipt["requested_effort"] is None


def test_route_drift_is_refused(tmp_path, codex_binary, monkeypatch):
    seen = {}
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()

    def exchange(_argv, requests, _timeout, *, env=None, followup_factory=None):
        seen["requests"] = requests
        start_response = {
            "id": 3,
            "result": {
                "thread": {"id": SESSION},
                "model": "wrong-model",
                "reasoningEffort": "xhigh",
                "cwd": os.path.realpath(tmp_path),
            },
        }
        followup_factory({3: start_response})
        stdout = "\n".join(
            [
                _response(1, {}),
                _response(
                    2,
                    {
                        "config": {
                            "projects": {os.path.realpath(tmp_path): {"trust_level": "trusted"}}
                        }
                    },
                ),
                json.dumps(start_response),
                _response(4, {}),
            ]
        )
        return stdout, "", 0

    monkeypatch.setattr(cnb, "_run_app_server_probe", exchange)
    path, digest = codex_binary
    with pytest.raises(cnb.CodexBootstrapError) as error:
        cnb.mint_session(
            codex_binary=path,
            binary_sha256=digest,
            version_output="codex-cli 0.146.0",
            working_directory=os.path.realpath(tmp_path),
            model="gpt-5.6-sol",
            effort="xhigh",
            profile_args=[],
            environment={"CODEX_HOME": str(codex_home)},
        )
    assert str(error.value) == (
        "Codex persistent thread resolved the wrong route or working directory: "
        "model='wrong-model' (expected 'gpt-5.6-sol')"
    )
    assert seen["requests"][-1]["params"]["model"] == "gpt-5.6-sol"


def test_bootstrap_refuses_when_materialization_leaves_no_rollout(
    tmp_path, codex_binary, monkeypatch
):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()

    def exchange(_argv, _requests, _timeout, *, env=None, followup_factory=None):
        start_response = {
            "id": 3,
            "result": {
                "thread": {"id": SESSION},
                "model": "gpt-5.6-sol",
                "reasoningEffort": "xhigh",
                "cwd": os.path.realpath(tmp_path),
            },
        }
        followup_factory({3: start_response})
        return (
            "\n".join(
                [
                    _response(1, {}),
                    _response(
                        2,
                        {
                            "config": {
                                "projects": {os.path.realpath(tmp_path): {"trust_level": "trusted"}}
                            }
                        },
                    ),
                    json.dumps(start_response),
                    _response(4, {}),
                ]
            ),
            "",
            0,
        )

    monkeypatch.setattr(cnb, "_run_app_server_probe", exchange)
    path, digest = codex_binary
    with pytest.raises(cnb.CodexBootstrapError, match="exactly one resumable rollout"):
        cnb.mint_session(
            codex_binary=path,
            binary_sha256=digest,
            version_output="codex-cli 0.146.0",
            working_directory=os.path.realpath(tmp_path),
            model="gpt-5.6-sol",
            effort="xhigh",
            profile_args=[],
            environment={"CODEX_HOME": str(codex_home)},
        )


def test_bootstrap_checks_protected_config_even_when_exchange_raises(
    tmp_path, codex_binary, monkeypatch
):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config = codex_home / "config.toml"
    config.write_text('model = "safe"\n')

    def exchange(_argv, _requests, _timeout, *, env=None, followup_factory=None):
        config.write_text('model = "mutated"\n')
        raise RuntimeError("app-server timed out")

    monkeypatch.setattr(cnb, "_run_app_server_probe", exchange)
    path, digest = codex_binary
    with pytest.raises(cnb.CodexBootstrapError, match="protected Codex user config changed"):
        cnb.mint_session(
            codex_binary=path,
            binary_sha256=digest,
            version_output="codex-cli 0.146.0",
            working_directory=os.path.realpath(tmp_path),
            model="gpt-5.6-sol",
            effort="xhigh",
            profile_args=[],
            environment={"CODEX_HOME": str(codex_home)},
        )


def test_bootstrap_intent_refuses_an_unmaterialized_legacy_receipt():
    with pytest.raises(cnb.CodexBootstrapError, match="materialized"):
        cnb.bootstrap_intent(
            {
                "schema": cnb.BOOTSTRAP_SCHEMA,
                "provider": "codex",
                "native_session_id": SESSION,
                "sent_no_turn": True,
                "detached_before_launch": True,
                "exit_proof": {"reaped": True},
            }
        )
