"""The Kiro v3 probe, version allow-list and command (#1167 Phase 1, gate G4).

v3 launches as ``/usr/bin/env -u … kiro-cli chat --agent-engine v3
--trust-workspace --trust-all-tools [--model] [--effort] --agent <name>``, and
every flag is probe-checked from the wrapper's own help before anything is
allocated. Only an allow-listed kiro-cli version, with a pinned engine schema,
may run v3; v2 is never affected. Provisional (#1167).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.providers.kiro_capabilities import (
    KIRO_V3_UNSET_ENV,
    KiroCapabilityError,
    build_kiro_command,
    kiro_effort,
    probe_kiro_capabilities,
    requested_kiro_capabilities,
)

FIXTURES = Path(__file__).parent / "fixtures"
HELP = {
    version: (
        (FIXTURES / f"kiro_cli_{version.replace('.', '_')}_root_help.txt").read_text(),
        (FIXTURES / f"kiro_cli_{version.replace('.', '_')}_chat_help.txt").read_text(),
    )
    for version in ("2.35.1", "2.36.0", "2.37.0")
}
V3_REQUEST = {"profile", "trust", "trust_workspace"}


def _runner(version="2.35.1", *, chat=None, root=None, version_text=None):
    root_help, chat_help = HELP.get(version, HELP["2.35.1"])
    root_help = root if root is not None else root_help
    chat_help = chat if chat is not None else chat_help
    version_text = version_text if version_text is not None else f"kiro-cli {version}\n"

    def run(command, **_kwargs):
        if command == ["kiro-cli", "--help"]:
            out = root_help
        elif command == ["kiro-cli", "chat", "--help"]:
            out = chat_help
        else:
            out = version_text
        return subprocess.CompletedProcess(command, 0, stdout=out, stderr="")

    return run


@pytest.mark.parametrize("version", ["2.35.1", "2.36.0", "2.37.0"])
def test_supported_releases_pass_the_v3_probe(version):
    caps = probe_kiro_capabilities(
        KiroEngine.V3, V3_REQUEST | {"model", "effort"}, runner=_runner(version)
    )
    assert caps.version == version
    assert "v3" in caps.agent_engines
    assert {"--agent-engine", "--trust-workspace", "--trust-all-tools", "--agent"} <= caps.flags
    assert {"--model", "--effort"} <= caps.flags
    assert "--v3" not in caps.flags


@pytest.mark.parametrize(
    "version_text", ["kiro-cli 2.38.0\n", "kiro-cli 2.35.0\n", "kiro-cli 3.0.0\n"]
)
def test_an_unknown_version_refuses_v3(version_text):
    with pytest.raises(KiroCapabilityError) as excinfo:
        probe_kiro_capabilities(
            KiroEngine.V3, V3_REQUEST, runner=_runner(version_text=version_text)
        )
    assert excinfo.value.kind == "kiro_v3_unsupported_version"
    assert "2.35.1" in str(excinfo.value) and "engine 'v2'" in str(excinfo.value)


def test_an_unparsable_version_refuses_v3():
    def run(command, **_kwargs):
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, stdout="unknown build\n", stderr="")
        return _runner()(command)

    with pytest.raises(KiroCapabilityError) as excinfo:
        probe_kiro_capabilities(KiroEngine.V3, V3_REQUEST, runner=run)
    assert excinfo.value.kind == "kiro_v3_unsupported_version"


def test_v2_is_never_affected_by_the_allow_list():
    caps = probe_kiro_capabilities(
        KiroEngine.V2, {"profile", "trust"}, runner=_runner(version_text="kiro-cli 9.9.9\n")
    )
    assert caps.version == "9.9.9"


def _without(text, flag):
    lines = text.splitlines(keepends=True)
    return "".join(line for line in lines if flag not in line)


@pytest.mark.parametrize(
    ("flag", "request_extra"),
    [
        ("--trust-workspace", set()),
        ("--trust-all-tools", set()),
        ("--agent <AGENT>", set()),
        ("--model <MODEL>", {"model"}),
        ("--effort <EFFORT>", {"effort"}),
    ],
)
def test_a_missing_flag_refuses_v3(flag, request_extra):
    chat = _without(HELP["2.35.1"][1], flag)
    with pytest.raises(KiroCapabilityError) as excinfo:
        probe_kiro_capabilities(
            KiroEngine.V3, V3_REQUEST | request_extra, runner=_runner(chat=chat)
        )
    assert excinfo.value.kind == "unsupported_capability"
    assert "Retry with engine 'v2'" in str(excinfo.value)


def test_v3_missing_from_the_engine_values_refuses():
    chat = HELP["2.35.1"][1].replace("[possible values: v2, v1, v3]", "[possible values: v2, v1]")
    assert chat != HELP["2.35.1"][1]
    with pytest.raises(KiroCapabilityError) as excinfo:
        probe_kiro_capabilities(KiroEngine.V3, V3_REQUEST, runner=_runner(chat=chat))
    assert excinfo.value.capability == "--agent-engine=v3"


def test_every_v3_request_asks_for_trust_and_trust_workspace():
    assert requested_kiro_capabilities(KiroEngine.V3, model=None, yolo=False) == V3_REQUEST
    assert requested_kiro_capabilities(KiroEngine.V3, model="m", yolo=True, effort="low") == (
        V3_REQUEST | {"model", "effort"}
    )
    assert requested_kiro_capabilities(KiroEngine.V2, model=None, yolo=True) == {"profile", "trust"}


def test_the_v3_argv_is_the_design_golden():
    assert build_kiro_command(KiroEngine.V3, "reviewer", model="m", effort="low") == [
        "/usr/bin/env",
        "-u",
        "ASBX_KIRO_MANDATORY_MCPS",
        "-u",
        "KIRO_AGENT_CONFIG_DIR",
        "-u",
        "KIRO_AGENT_ENGINE",
        "-u",
        "KIRO_HOME",
        "kiro-cli",
        "chat",
        "--agent-engine",
        "v3",
        "--trust-workspace",
        "--trust-all-tools",
        "--model",
        "m",
        "--effort",
        "low",
        "--agent",
        "reviewer",
    ]
    assert build_kiro_command(KiroEngine.V3, "reviewer", yolo=False)[-3:] == [
        "--trust-all-tools",
        "--agent",
        "reviewer",
    ]
    assert KIRO_V3_UNSET_ENV == (
        "ASBX_KIRO_MANDATORY_MCPS",
        "KIRO_AGENT_CONFIG_DIR",
        "KIRO_AGENT_ENGINE",
        "KIRO_HOME",
    )


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, ["kiro-cli", "chat", "--agent-engine", "v2", "--agent", "dev"]),
        (
            {"yolo": True},
            ["kiro-cli", "chat", "--agent-engine", "v2", "--trust-all-tools", "--agent", "dev"],
        ),
        (
            {"model": "m"},
            ["kiro-cli", "chat", "--agent-engine", "v2", "--model", "m", "--agent", "dev"],
        ),
        (
            {"model": "m", "yolo": True},
            [
                "kiro-cli",
                "chat",
                "--agent-engine",
                "v2",
                "--trust-all-tools",
                "--model",
                "m",
                "--agent",
                "dev",
            ],
        ),
    ],
)
def test_the_v2_argv_is_unchanged(kwargs, expected):
    """Frozen from main: no v2 launch changes."""
    assert build_kiro_command(KiroEngine.V2, "dev", **kwargs) == expected


@pytest.mark.parametrize("value", ["low", "medium", "high", "xhigh", "max"])
def test_kiro_effort_reads_kiro_config(value):
    assert (
        kiro_effort(AgentProfile(name="a", description="d", kiroConfig={"effort": value})) == value
    )


@pytest.mark.parametrize("config", [None, {}, {"effort": None}])
def test_no_kiro_effort_without_kiro_config_effort(config):
    assert kiro_effort(AgentProfile(name="a", description="d", kiroConfig=config)) is None


@pytest.mark.parametrize("bad", ["--agent", "High", "", "low high", 3])
def test_a_kiro_effort_that_could_be_another_flag_is_refused(bad):
    with pytest.raises(ValueError, match="kiroConfig.effort"):
        kiro_effort(AgentProfile(name="a", description="d", kiroConfig={"effort": bad}))


@pytest.mark.asyncio
async def test_create_refuses_an_unsupported_version_before_allocation():
    from cli_agent_orchestrator.services import terminal_service

    _TS = "cli_agent_orchestrator.services.terminal_service"

    def probe(engine, requested):
        return probe_kiro_capabilities(
            engine, requested, runner=_runner(version_text="kiro-cli 2.40.0\n")
        )

    with (
        patch(
            f"{_TS}.agent_profiles.load_agent_profile",
            return_value=AgentProfile(name="r", description="d"),
        ),
        patch(f"{_TS}.get_backend") as backend,
        patch(f"{_TS}.db_create_terminal") as db_create,
        patch(f"{_TS}.fifo_manager") as fifo,
        patch(f"{_TS}.provider_manager") as providers,
    ):
        with pytest.raises(KiroCapabilityError) as excinfo:
            await terminal_service.create_terminal(
                provider="kiro_cli",
                agent_profile="r",
                new_session=True,
                engine="v3",
                kiro_capability_probe=probe,
            )
    assert excinfo.value.kind == "kiro_v3_unsupported_version"
    backend.return_value.create_window.assert_not_called()
    backend.return_value.create_session.assert_not_called()
    db_create.assert_not_called()
    fifo.create_reader.assert_not_called()
    providers.create_provider.assert_not_called()


@pytest.mark.asyncio
async def test_a_pending_version_refuses_v3_before_allocation():
    """2.36.0 is allow-listed but pending operator verification (#1167): not admitted."""
    from cli_agent_orchestrator.providers.kiro_v3_schema import KiroV3UnverifiedVersionError
    from cli_agent_orchestrator.services import terminal_service

    _TS = "cli_agent_orchestrator.services.terminal_service"

    def probe(engine, requested):
        return probe_kiro_capabilities(engine, requested, runner=_runner("2.36.0"))

    with (
        patch(
            f"{_TS}.agent_profiles.load_agent_profile",
            return_value=AgentProfile(name="r", description="d", allowedTools=["fs_read"]),
        ),
        patch(f"{_TS}.get_backend") as backend,
        patch(f"{_TS}.db_create_terminal") as db_create,
    ):
        with pytest.raises(KiroV3UnverifiedVersionError, match="pending operator verification"):
            await terminal_service.create_terminal(
                provider="kiro_cli",
                agent_profile="r",
                new_session=True,
                engine="kas",
                kiro_capability_probe=probe,
            )
    backend.return_value.create_window.assert_not_called()
    db_create.assert_not_called()
