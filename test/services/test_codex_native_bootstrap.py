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


def test_the_bootstrap_holds_no_build_allowlist():
    """Capability comes from the exchange, not from a list of builds.

    An allowlist answers False for every build nobody has listed, so it
    expires on the vendor's release schedule rather than on evidence. The
    contract this bootstrap claims is checkable while it runs, so it is
    checked while it runs — including the one leg the minting process
    cannot establish about itself.
    """
    assert not hasattr(cnb, "BOOTSTRAP_CAPABLE_VERSIONS")
    assert not hasattr(cnb, "is_bootstrap_capable_build")
    assert cnb.RESUME_ADOPTION_SCHEMA == "cao-codex-native-resume-adoption-v1"
    assert cnb.RESUME_METHOD == "thread/resume"


def test_bootstrap_materializes_a_resumable_zero_turn_thread(tmp_path, codex_binary, monkeypatch):
    seen = {}
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()

    def exchange(argv, requests, timeout, *, env=None, followup_factory=None):
        if followup_factory is None:
            # The fresh-process resume-adoption leg: no followup factory,
            # because its one request needs nothing from an earlier response.
            globals().setdefault("_last_adoption", requests)
            return (
                "\n".join(
                    [
                        _response(1, {"userAgent": "codex-test"}),
                        _response(
                            2,
                            {
                                "thread": {
                                    "id": SESSION,
                                    "sessionId": SESSION,
                                    "ephemeral": False,
                                }
                            },
                        ),
                    ]
                ),
                "",
                0,
            )
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
        if followup_factory is None:
            return (
                "\n".join(
                    [
                        _response(1, {"userAgent": "codex-test"}),
                        _response(
                            2,
                            {
                                "thread": {
                                    "id": SESSION,
                                    "sessionId": SESSION,
                                    "ephemeral": False,
                                }
                            },
                        ),
                    ]
                ),
                "",
                0,
            )
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


def _mint_with_adoption(tmp_path, codex_binary, monkeypatch, adoption):
    """Run a clean mint whose fresh-process adoption leg returns ``adoption``.

    ``adoption`` is ``(stdout, stderr, returncode)`` for the second probe.
    """
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()

    def exchange(argv, requests, timeout, *, env=None, followup_factory=None):
        if followup_factory is None:
            return adoption
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
        rollout = codex_home / "sessions" / "2026" / "07" / "30"
        rollout.mkdir(parents=True, exist_ok=True)
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
            -15,
        )

    monkeypatch.setattr(cnb, "_run_app_server_probe", exchange)
    path, digest = codex_binary
    return cnb.mint_session(
        codex_binary=path,
        binary_sha256=digest,
        version_output="codex-cli 0.999.0",
        working_directory=os.path.realpath(tmp_path),
        model="gpt-5.6-sol",
        effort="xhigh",
        profile_args=["--yolo"],
        environment={"PATH": os.environ["PATH"], "CODEX_HOME": str(codex_home)},
    )


def _adoption_stdout(thread):
    return "\n".join([_response(1, {"userAgent": "codex-test"}), _response(2, {"thread": thread})])


def test_an_unlisted_build_mints_when_it_proves_the_whole_contract(
    tmp_path, codex_binary, monkeypatch
):
    """0.999.0 is on no list anywhere and mints, because it did the work.

    This is the entire point of removing the allowlist: the build in front of
    the process either honours the contract or it does not, and that is
    observable without anyone having tested it first.
    """
    receipt = _mint_with_adoption(
        tmp_path,
        codex_binary,
        monkeypatch,
        (_adoption_stdout({"id": SESSION, "sessionId": SESSION, "ephemeral": False}), "", 0),
    )
    proof = receipt["resume_adoption_proof"]
    assert proof["schema"] == cnb.RESUME_ADOPTION_SCHEMA
    assert proof["adopted_session_id"] == SESSION
    assert proof["adopted_in_fresh_process"] is True
    assert proof["sent_no_turn"] is True
    assert "bootstrap_capable_versions" not in receipt


def test_a_build_that_cannot_be_resumed_by_another_process_fails_closed(
    tmp_path, codex_binary, monkeypatch
):
    """The leg the minting process cannot prove about itself.

    ``thread/name/set`` materializes a rollout inside one app-server
    lifetime. Without this leg a mint could report a resumable session that
    only its own process could ever open, which is the precise falsehood the
    old allowlist was standing in for.
    """
    with pytest.raises(cnb.CodexBootstrapError, match="thread/resume failed"):
        _mint_with_adoption(
            tmp_path,
            codex_binary,
            monkeypatch,
            (
                "\n".join(
                    [
                        _response(1, {"userAgent": "codex-test"}),
                        json.dumps(
                            {"id": 2, "error": {"code": -32600, "message": "no rollout found"}}
                        ),
                    ]
                ),
                "",
                0,
            ),
        )


def test_adopting_a_different_thread_is_refused(tmp_path, codex_binary, monkeypatch):
    other = "01a01a53-0000-7000-8000-000000000000"
    with pytest.raises(cnb.CodexBootstrapError, match="adopted"):
        _mint_with_adoption(
            tmp_path,
            codex_binary,
            monkeypatch,
            (_adoption_stdout({"id": other, "ephemeral": False}), "", 0),
        )


def test_a_session_id_disagreeing_with_the_thread_is_refused(tmp_path, codex_binary, monkeypatch):
    other = "01a01a53-0000-7000-8000-000000000000"
    with pytest.raises(cnb.CodexBootstrapError, match="sessionId"):
        _mint_with_adoption(
            tmp_path,
            codex_binary,
            monkeypatch,
            (
                _adoption_stdout({"id": SESSION, "sessionId": other, "ephemeral": False}),
                "",
                0,
            ),
        )


def test_an_ephemeral_adoption_is_refused(tmp_path, codex_binary, monkeypatch):
    """An ephemeral thread is non-resumable by construction."""
    with pytest.raises(cnb.CodexBootstrapError, match="ephemeral"):
        _mint_with_adoption(
            tmp_path,
            codex_binary,
            monkeypatch,
            (_adoption_stdout({"id": SESSION, "ephemeral": True}), "", 0),
        )


def test_a_build_that_omits_session_id_is_not_refused_for_the_omission(
    tmp_path, codex_binary, monkeypatch
):
    """Judge behaviour, not response shape.

    Asserting a field this build does not carry would refuse a working resume
    for its schema rather than its conduct — the same failure mode as the
    allowlist, one level down.
    """
    receipt = _mint_with_adoption(
        tmp_path,
        codex_binary,
        monkeypatch,
        (_adoption_stdout({"id": SESSION, "ephemeral": False}), "", 0),
    )
    assert receipt["resume_adoption_proof"]["reported_session_id"] is None


def test_a_dirty_exit_from_the_adoption_probe_is_refused(tmp_path, codex_binary, monkeypatch):
    with pytest.raises(cnb.CodexBootstrapError, match="exited 3 during resume adoption"):
        _mint_with_adoption(
            tmp_path,
            codex_binary,
            monkeypatch,
            (
                _adoption_stdout({"id": SESSION, "sessionId": SESSION, "ephemeral": False}),
                "boom",
                3,
            ),
        )


def test_the_adoption_probe_refuses_if_it_rewrote_the_protected_config(
    tmp_path, codex_binary, monkeypatch
):
    """A proof bought with a side effect nobody asked for is not a proof."""
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config = codex_home / "config.toml"
    config.write_text('model = "gpt-5.6-sol"\n')

    def exchange(argv, requests, timeout, *, env=None, followup_factory=None):
        if followup_factory is None:
            config.write_text('model = "something-else"\n')
            return (
                _adoption_stdout({"id": SESSION, "sessionId": SESSION, "ephemeral": False}),
                "",
                0,
            )
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
        rollout = codex_home / "sessions" / "2026" / "07" / "30"
        rollout.mkdir(parents=True, exist_ok=True)
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
            -15,
        )

    monkeypatch.setattr(cnb, "_run_app_server_probe", exchange)
    path, digest = codex_binary
    with pytest.raises(cnb.CodexBootstrapError, match="config changed during the resume-adoption"):
        cnb.mint_session(
            codex_binary=path,
            binary_sha256=digest,
            version_output="codex-cli 0.999.0",
            working_directory=os.path.realpath(tmp_path),
            model="gpt-5.6-sol",
            effort="xhigh",
            profile_args=["--yolo"],
            environment={"PATH": os.environ["PATH"], "CODEX_HOME": str(codex_home)},
        )
