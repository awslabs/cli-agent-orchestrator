"""The ephemeral helper's v3 command and probe (#1167 Phase 2, gate G18, command part).

A helper launches as ``/usr/bin/env -u ASBX_KIRO_MANDATORY_MCPS -u
KIRO_AGENT_CONFIG_DIR -u KIRO_AGENT_ENGINE KIRO_HOME=<home> <absolute kiro-cli>
chat --agent-engine v3 --trust-workspace --trust-all-tools [--model] [--effort]
--agent <name>``, and the probe runs the same binary under the probe home. With
neither option set, every installed command is unchanged. kiro-cli is never run:
the env check execs a stand-in script.
"""

from __future__ import annotations

import os
import subprocess
from test.providers.test_kiro_v3_probe import HELP, V3_REQUEST

import pytest

from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.providers.kiro_capabilities import (
    build_kiro_command,
    probe_kiro_capabilities,
)

KIRO = "/Users/op/.local/bin/kiro-cli"
HOME = "/Users/op/.aws/cli-agent-orchestrator/ephemeral/kiro/0a1b2c3d"


def helper_command(**kwargs):
    return build_kiro_command(
        KiroEngine.V3, "Heart-x-3f9a", executable=KIRO, kiro_home=HOME, **kwargs
    )


def test_the_helper_argv_is_the_design_golden():
    assert helper_command(model="m", effort="low") == [
        "/usr/bin/env",
        "-u",
        "ASBX_KIRO_MANDATORY_MCPS",
        "-u",
        "KIRO_AGENT_CONFIG_DIR",
        "-u",
        "KIRO_AGENT_ENGINE",
        f"KIRO_HOME={HOME}",
        KIRO,
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
        "Heart-x-3f9a",
    ]


def test_the_home_is_assigned_after_the_unsets_and_never_unset():
    argv = helper_command()
    binary = argv.index(KIRO)
    prefix = argv[:binary]
    assert prefix[0] == "/usr/bin/env"
    assert [prefix[i + 1] for i, word in enumerate(prefix) if word == "-u"] == [
        "ASBX_KIRO_MANDATORY_MCPS",
        "KIRO_AGENT_CONFIG_DIR",
        "KIRO_AGENT_ENGINE",
    ]
    assert prefix[-1] == f"KIRO_HOME={HOME}"
    assert sum(word.startswith("KIRO_HOME=") for word in argv) == 1


def test_env_applies_the_home_over_an_rc_export_and_drops_forced_mcp(tmp_path):
    """The real /usr/bin/env, exec'ing a stand-in that prints what kiro-cli would see."""
    stand_in = tmp_path / "kiro-cli"
    stand_in.write_text(
        "#!/bin/sh\n"
        'echo "KIRO_HOME=$KIRO_HOME"\n'
        'echo "ASBX=${ASBX_KIRO_MANDATORY_MCPS-unset}"\n'
        'echo "CONFIG=${KIRO_AGENT_CONFIG_DIR-unset}"\n'
        'echo "ENGINE=${KIRO_AGENT_ENGINE-unset}"\n'
    )
    stand_in.chmod(0o700)
    home = str(tmp_path / "home")
    argv = build_kiro_command(KiroEngine.V3, "a", executable=str(stand_in), kiro_home=home)
    prefix = argv[: argv.index(str(stand_in)) + 1]
    rc_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "KIRO_HOME": "/Users/op/.kiro",
        "ASBX_KIRO_MANDATORY_MCPS": "forced-server",
        "KIRO_AGENT_CONFIG_DIR": "/elsewhere",
        "KIRO_AGENT_ENGINE": "v2",
    }
    out = subprocess.run(prefix, env=rc_env, capture_output=True, text=True, check=True).stdout
    assert out.splitlines() == [
        f"KIRO_HOME={home}",
        "ASBX=unset",
        "CONFIG=unset",
        "ENGINE=unset",
    ]


def test_installed_commands_are_unchanged_by_default():
    installed = build_kiro_command(KiroEngine.V3, "reviewer", model="m")
    assert installed[:9] == ["/usr/bin/env", "-u", "ASBX_KIRO_MANDATORY_MCPS", "-u"] + [
        "KIRO_AGENT_CONFIG_DIR",
        "-u",
        "KIRO_AGENT_ENGINE",
        "-u",
        "KIRO_HOME",
    ]
    assert installed[9] == "kiro-cli"
    assert not any(word.startswith("KIRO_HOME=") for word in installed)
    assert build_kiro_command(KiroEngine.V2, "dev", yolo=True)[0] == "kiro-cli"


@pytest.mark.parametrize("kwargs", [{"executable": KIRO}, {"kiro_home": HOME}])
def test_the_per_launch_options_are_v3_only(kwargs):
    with pytest.raises(ValueError):
        build_kiro_command(KiroEngine.V2, "dev", yolo=True, **kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"executable": "bin/kiro-cli"},
        {"kiro_home": "relative/home"},
        {"kiro_home": ""},
        # A helper's KIRO_HOME with the default bare kiro-cli would resolve
        # through the pane's PATH.
        {"kiro_home": HOME},
    ],
)
def test_the_per_launch_paths_must_be_absolute(kwargs):
    with pytest.raises(ValueError):
        build_kiro_command(KiroEngine.V3, "a", **kwargs)


def test_the_probe_runs_the_launch_binary_under_the_given_env():
    root_help, chat_help = HELP["2.35.1"]
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs.get("env")))
        out = {("--help",): root_help, ("chat", "--help"): chat_help}.get(
            tuple(command[1:]), "kiro-cli 2.35.1\n"
        )
        return subprocess.CompletedProcess(command, 0, stdout=out, stderr="")

    env = {"KIRO_HOME": "/cao/ephemeral/kiro-probe", "PATH": "/usr/bin"}
    probe_kiro_capabilities(KiroEngine.V3, V3_REQUEST, runner=run, executable=KIRO, env=env)
    assert [command for command, _ in calls] == [
        [KIRO, "--help"],
        [KIRO, "chat", "--help"],
        [KIRO, "--version"],
    ]
    assert all(seen == env for _, seen in calls)

    calls.clear()
    probe_kiro_capabilities(KiroEngine.V3, V3_REQUEST, runner=run)
    assert [command[0] for command, _ in calls] == ["kiro-cli"] * 3
    assert all(seen is None for _, seen in calls)


def test_a_kiro_cli_path_holding_equals_is_refused(tmp_path):
    """env reads ``…/dir=x/kiro-cli`` as NAME=VALUE and would exec ``chat`` instead."""
    stand_in = tmp_path / "dir=x" / "kiro-cli"
    stand_in.parent.mkdir()
    stand_in.write_text("#!/bin/sh\necho launched\n")
    stand_in.chmod(0o700)
    with pytest.raises(ValueError):
        build_kiro_command(KiroEngine.V3, "a", executable=str(stand_in), kiro_home=HOME)
    # What the refusal prevents, on the real env: the stand-in never runs.
    argv = ["/usr/bin/env", f"KIRO_HOME={tmp_path}", str(stand_in), "chat"]
    run = subprocess.run(argv, env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True)
    assert "launched" not in run.stdout and run.returncode != 0
