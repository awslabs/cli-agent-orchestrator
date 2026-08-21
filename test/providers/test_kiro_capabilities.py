"""Deterministic Phase 0 wrapper-probe and command tests."""

import subprocess
from pathlib import Path

import pytest

from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.providers.kiro_capabilities import (
    KiroCapabilityError,
    _flags_from_help,
    build_kiro_command,
    probe_kiro_capabilities,
)

_HELP = (
    "kiro-cli version 2.13.0\n"
    "--agent-engine v2|v1|v3\n--v3\n--agent NAME\n--model MODEL\n"
    "--legacy-ui\n--trust-all-tools\n--require-mcp-startup\n"
)
_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_RELEASED_ROOT_HELP = (_FIXTURES_DIR / "kiro_cli_2_13_0_root_help.txt").read_text(encoding="utf-8")
_RELEASED_CHAT_HELP = (_FIXTURES_DIR / "kiro_cli_2_13_0_chat_help.txt").read_text(encoding="utf-8")


def _runner(command, **_kwargs):
    output = "kiro-cli version 2.13.0" if command[-1] == "--version" else _HELP
    return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")


def test_probe_accepts_v2_and_requested_wrapper_features():
    result = probe_kiro_capabilities(
        KiroEngine.V2, {"profile", "model", "ui", "trust"}, runner=_runner
    )

    assert result.version == "2.13.0"
    assert result.supports("--agent-engine")
    assert result.agent_engines == frozenset({"v1", "v2", "v3"})


def test_probe_accepts_released_wrapper_help_before_explicit_v2_construction():
    """The released wrapper splits root commands from chat engine capabilities."""
    calls = []

    def released_wrapper_runner(command, **kwargs):
        calls.append((command, kwargs))
        if command == ["kiro-cli", "--help"]:
            output = _RELEASED_ROOT_HELP
        elif command == ["kiro-cli", "chat", "--help"]:
            output = _RELEASED_CHAT_HELP
        else:
            output = "kiro-cli version 2.13.0"
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    capabilities = probe_kiro_capabilities(
        KiroEngine.V2,
        {"profile", "model", "ui", "trust", "mcp_startup"},
        timeout=1.0,
        chat_help_timeout=7.5,
        runner=released_wrapper_runner,
    )

    assert capabilities.version == "2.13.0"
    assert capabilities.agent_engines == frozenset({"v1", "v2", "v3"})
    assert build_kiro_command(KiroEngine.V2, "developer") == [
        "kiro-cli",
        "chat",
        "--agent-engine",
        "v2",
        "--agent",
        "developer",
    ]
    assert calls == [
        (
            ["kiro-cli", "--help"],
            {"capture_output": True, "text": True, "timeout": 1.0},
        ),
        (
            ["kiro-cli", "chat", "--help"],
            {
                "capture_output": True,
                "text": True,
                "timeout": 7.5,
                "stdin": subprocess.DEVNULL,
            },
        ),
        (
            ["kiro-cli", "--version"],
            {"capture_output": True, "text": True, "timeout": 1.0},
        ),
    ]


def test_probe_rejects_required_flag_mentioned_only_in_prose():
    def prose_only_flag_runner(command, **_kwargs):
        output = (
            "--agent-engine v2\n--agent NAME\n--legacy-ui\n"
            "Use --trust-all-tools to bypass confirmation.\n"
            if command == ["kiro-cli", "chat", "--help"]
            else "kiro-cli version 2.13.0"
        )
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    with pytest.raises(KiroCapabilityError, match="--trust-all-tools") as exc_info:
        probe_kiro_capabilities(
            KiroEngine.V2,
            {"profile", "ui", "trust"},
            runner=prose_only_flag_runner,
        )

    assert exc_info.value.capability == "--trust-all-tools"


@pytest.mark.parametrize(
    "flag",
    [
        "--agent-engine",
        "--v3",
        "--agent",
        "--model",
        "--legacy-ui",
        "--trust-all-tools",
        "--require-mcp-startup",
    ],
)
def test_flags_from_help_rejects_leading_prose_for_every_requested_capability(flag):
    """Capability tokens at the start of prose are not option declarations."""
    assert _flags_from_help(f"{flag} bypasses confirmation when enabled") == frozenset()


def test_flags_from_help_rejects_short_bare_agent_engine_prose():
    assert _flags_from_help("--agent-engine bypasses") == frozenset()


@pytest.mark.parametrize(
    "flag",
    [
        "--v3",
        "--legacy-ui",
        "--trust-all-tools",
        "--require-mcp-startup",
    ],
)
@pytest.mark.parametrize("suffix", ["...", " VALUE"])
def test_flags_from_help_rejects_incompatible_bare_boolean_declarations(flag, suffix):
    assert _flags_from_help(f"{flag}{suffix}") == frozenset()


@pytest.mark.parametrize(
    "flag",
    [
        "--agent-engine",
        "--v3",
        "--agent",
        "--model",
        "--legacy-ui",
        "--trust-all-tools",
        "--require-mcp-startup",
    ],
)
def test_flags_from_help_rejects_ellipsis_suffixed_requested_candidates(flag):
    assert _flags_from_help(f"{flag}...") == frozenset()


def test_flags_from_help_accepts_formal_short_long_metavariable_alias_and_variadic_forms():
    flags = _flags_from_help(
        "\n".join(
            [
                "--agent-engine <ENGINE>",
                "--v3",
                "--agent <AGENT>",
                "--model MODEL",
                "--legacy-ui, --legacy",
                "-a, --trust-all-tools",
                "-v, --verbose...",
                "--require-mcp-startup",
            ]
        )
    )

    assert flags == frozenset(
        {
            "--agent-engine",
            "--v3",
            "--agent",
            "--model",
            "--legacy-ui",
            "--trust-all-tools",
            "--require-mcp-startup",
        }
    )


def test_flags_from_help_does_not_advertise_requested_capabilities_from_verbose_variadic_form():
    assert _flags_from_help("-v, --verbose...") == frozenset()


def test_probe_rejects_root_prose_and_chat_prose_only_agent_engine_values():
    def prose_only_engine_runner(command, **_kwargs):
        if command == ["kiro-cli", "--help"]:
            output = "Use --agent-engine <ENGINE> with possible values: v2, v1, v3.\n"
        elif command == ["kiro-cli", "chat", "--help"]:
            output = (
                "--agent-engine <ENGINE>\n"
                "Agent engine configuration is documented elsewhere.\n"
                "The supported possible values: v2, v1, v3.\n"
                "--agent NAME\n--legacy-ui\n"
            )
        else:
            output = "kiro-cli version 2.13.0"
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    with pytest.raises(KiroCapabilityError, match="explicit 'v2'") as exc_info:
        probe_kiro_capabilities(
            KiroEngine.V2,
            {"profile", "ui"},
            runner=prose_only_engine_runner,
        )

    assert exc_info.value.capability == "--agent-engine=v2"


@pytest.mark.parametrize(
    "declaration",
    [
        "v2",
        "<v1|v2|v3>",
        "[v1|v2|v3]",
        "{v1,v2,v3}",
    ],
)
def test_probe_accepts_explicit_v2_agent_engine_declarations(declaration):
    def v2_declaration_runner(command, **_kwargs):
        output = (
            f"--agent-engine {declaration}\n--agent NAME\n--legacy-ui\n"
            if command[-1] == "--help"
            else "kiro-cli 2.13.0"
        )
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    result = probe_kiro_capabilities(
        KiroEngine.V2,
        {"profile", "ui"},
        runner=v2_declaration_runner,
    )

    assert result.supports_agent_engine(KiroEngine.V2)


def test_probe_rejects_agent_engine_declaration_that_excludes_v2():
    def v1_v3_only_runner(command, **_kwargs):
        output = (
            "--agent-engine v1|v3\n--agent NAME\n--legacy-ui\n"
            if command[-1] == "--help"
            else "kiro-cli 2.13.0"
        )
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    with pytest.raises(KiroCapabilityError, match="accept 'v2'") as exc_info:
        probe_kiro_capabilities(
            KiroEngine.V2,
            {"profile", "ui"},
            runner=v1_v3_only_runner,
        )

    assert exc_info.value.kind == "unsupported_capability"
    assert exc_info.value.capability == "--agent-engine=v2"


@pytest.mark.parametrize(
    "declaration",
    [
        "--agent-engine",
        "--agent-engine <v1|v2",
        "--agent-engine v1|v2/",
    ],
)
def test_probe_rejects_malformed_agent_engine_declarations(declaration):
    def malformed_declaration_runner(command, **_kwargs):
        output = (
            f"{declaration}\n--agent NAME\n--legacy-ui\n"
            if command[-1] == "--help"
            else "kiro-cli 2.13.0"
        )
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    with pytest.raises(KiroCapabilityError, match="explicit 'v2'") as exc_info:
        probe_kiro_capabilities(
            KiroEngine.V2,
            {"profile", "ui"},
            runner=malformed_declaration_runner,
        )

    assert exc_info.value.kind == "unsupported_capability"
    assert exc_info.value.capability == "--agent-engine=v2"


def test_probe_uses_bounded_root_chat_and_version_wrapper_contracts():
    commands = []

    def recording_runner(command, **_kwargs):
        commands.append(command)
        return _runner(command)

    probe_kiro_capabilities(KiroEngine.V2, {"profile"}, runner=recording_runner)

    assert commands == [
        ["kiro-cli", "--help"],
        ["kiro-cli", "chat", "--help"],
        ["kiro-cli", "--version"],
    ]


def test_probe_rejects_unsupported_kas_selector_without_fallback():
    def no_v3(command, **_kwargs):
        output = "--agent-engine\n--agent\n" if command[-1] == "--help" else "kiro-cli 2.13.0"
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    with pytest.raises(KiroCapabilityError, match="--v3") as exc_info:
        probe_kiro_capabilities(KiroEngine.KAS, {"profile"}, runner=no_v3)

    assert exc_info.value.kind == "unsupported_capability"
    assert exc_info.value.engine == KiroEngine.KAS


@pytest.mark.parametrize(
    ("runner", "kind"),
    [
        (
            lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError()),
            "missing_executable",
        ),
        (
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                subprocess.TimeoutExpired("kiro-cli", 1)
            ),
            "timeout",
        ),
        (
            lambda command, **_kwargs: subprocess.CompletedProcess(
                command, 0, stdout="", stderr=""
            ),
            "malformed_output",
        ),
    ],
)
def test_probe_reports_distinct_execution_failures(runner, kind):
    with pytest.raises(KiroCapabilityError) as exc_info:
        probe_kiro_capabilities(KiroEngine.V2, set(), runner=runner)
    assert exc_info.value.kind == kind


def test_builds_explicit_v2_and_deterministic_kas_commands():
    assert build_kiro_command(
        KiroEngine.V2, "developer", model="fixture-model", yolo=True, legacy_ui=True
    ) == [
        "kiro-cli",
        "chat",
        "--agent-engine",
        "v2",
        "--legacy-ui",
        "--trust-all-tools",
        "--model",
        "fixture-model",
        "--agent",
        "developer",
    ]
    assert build_kiro_command(KiroEngine.KAS, "developer") == [
        "kiro-cli",
        "--v3",
        "chat",
        "--agent",
        "developer",
    ]
