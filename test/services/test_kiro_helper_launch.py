"""An ephemeral Kiro helper's launch on v3, through create_terminal (#1167 Phase 2).

The registry and the ephemeral store are real (in-memory SQLite, files under
tmp_path); the backend and the provider are doubles. kiro-cli is a fake script
on PATH that answers only ``--help``, ``chat --help`` and ``--version`` with
kiro-cli 2.35.1's captured help, so the real probe runs against it. Nothing
here runs Kiro, and the operator's ``~/.kiro`` is a fake HOME's that only
``stat`` may touch (``operator_home``). Refusal rules are provisional (#1167):
the tests check which rule fires and what was (not) allocated.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from test.providers.kiro_helper_support import operator_home  # noqa: F401
from test.services.test_ephemeral_claim import audit, claim  # noqa: F401
from test.services.test_ephemeral_service import CALLER, SPEC, create, create_store  # noqa: F401
from unittest.mock import AsyncMock, Mock

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.ephemeral import TOOL_ATOMS
from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.providers import kiro_launch_home
from cli_agent_orchestrator.providers.kiro_capabilities import (
    KiroCapabilityError,
    probe_kiro_capabilities,
)
from cli_agent_orchestrator.providers.kiro_launch_home import KiroLaunchRefusal
from cli_agent_orchestrator.services import kiro_v3_launch, session_env, terminal_service
from cli_agent_orchestrator.utils import ephemeral_capabilities as caps

CHILD = "eeeeeeee"
FIXTURES = Path(__file__).parents[1] / "providers" / "fixtures"

FAKE_KIRO = """#!{python}
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "calls.jsonl"), "a") as log:
    log.write(json.dumps({{"argv": sys.argv, "home": os.environ.get("KIRO_HOME", "")}}) + "\\n")
version_file = os.path.join(here, "version")
version = open(version_file).read().strip() if os.path.exists(version_file) else "2.35.1"
args = sys.argv[1:]
if args == ["--help"]:
    sys.stdout.write(open({root!r}, encoding="utf-8").read())
elif args == ["chat", "--help"]:
    sys.stdout.write(open({chat!r}, encoding="utf-8").read())
elif args == ["--version"]:
    sys.stdout.write("kiro-cli " + version + "\\n")
else:
    sys.exit(2)
"""


def _kiro_row() -> caps.EphemeralCapability:
    # The row the capability table gains in a later commit; patched in so the
    # helper launch is testable before kiro_cli is admitted to rule 8.
    return caps.EphemeralCapability(
        caps.HARD, frozenset(TOOL_ATOMS), True, ("kiroConfig", "effort")
    )


class SimpleEnv:
    def __init__(self, **values):
        self.__dict__.update(values)

    def calls(self):
        log = self.fake / "calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _unseal_tree(root: Path) -> None:
    for directory, dirnames, _ in os.walk(root):
        for name in dirnames:
            path = Path(directory) / name
            if not path.is_symlink() and stat.S_IMODE(os.lstat(path).st_mode) == 0:
                path.chmod(0o700)


@pytest.fixture
def kiro(create_store, operator_home, tmp_path, monkeypatch):  # noqa: F811
    env = create_store
    service, _, cao_home, _, config = env
    config["ephemeral"]["allowed_providers"] = ["claude_code", "kiro_cli"]
    monkeypatch.setitem(caps.CAPABILITIES, "kiro_cli", _kiro_row())
    name = create(env, {**SPEC, "provider": "kiro_cli", "tools": ["fs_read", "fs_list"]})["name"]

    fake = tmp_path / "fake-bin"
    fake.mkdir()
    script = fake / "kiro-cli"
    script.write_text(
        FAKE_KIRO.format(
            python=sys.executable,
            root=str(FIXTURES / "kiro_cli_2_35_1_root_help.txt"),
            chat=str(FIXTURES / "kiro_cli_2_35_1_chat_help.txt"),
        )
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}{os.pathsep}{os.environ.get('PATH', '')}")

    repo = tmp_path / "repo"
    repo.mkdir()
    seen: dict = {}

    def create_window(session, window, terminal_id, cwd, extra_env=None, **kwargs):
        seen["home_existed"] = kiro_launch_home.kiro_home_path(CHILD).is_dir()
        seen["window_env"] = dict(extra_env or {})
        return "child"

    def create_session(session, window, terminal_id, cwd, extra_env=None, **kwargs):
        seen["home_existed"] = kiro_launch_home.kiro_home_path(CHILD).is_dir()
        seen["window_env"] = dict(extra_env or {})

    backend = Mock()
    backend.session_exists.return_value = True
    backend.create_window.side_effect = create_window
    backend.create_session.side_effect = create_session
    backend.supports_event_inbox.return_value = True
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(terminal_service, "get_max_terminals", lambda: None)
    monkeypatch.setattr(terminal_service, "generate_terminal_id", lambda: CHILD)
    monkeypatch.setattr(terminal_service, "build_skill_catalog", lambda p: None)
    monkeypatch.setattr(terminal_service, "get_herdr_inbox_service", lambda: None)
    provider = Mock(
        initialize=AsyncMock(return_value=True),
        identity_verified=True,
        shell_baseline=None,
        runtime_variant=None,
    )
    factory = Mock(return_value=provider)
    monkeypatch.setattr(terminal_service.provider_manager, "create_provider", factory)
    probe = Mock(wraps=probe_kiro_capabilities)
    yield SimpleEnv(
        env=env,
        service=service,
        cao_home=cao_home,
        name=name,
        repo=repo,
        fake=fake,
        script=script,
        backend=backend,
        provider=provider,
        factory=factory,
        probe=probe,
        seen=seen,
        operator=operator_home,
    )
    kiro_launch_home.discard_launch_home(CHILD)
    session_env.clear_session_env("cao-fresh")
    _unseal_tree(cao_home)


async def launch(kiro, **kwargs):
    args = dict(
        session_name="cao-session",
        caller_id=CALLER,
        working_directory=str(kiro.repo),
        kiro_capability_probe=kiro.probe,
    )
    args.update(kwargs)
    return await terminal_service.create_terminal("kiro_cli", kiro.name, **args)


def home():
    return kiro_launch_home.kiro_home_path(CHILD)


def assert_refused_before_bind(kiro):
    assert database.get_ephemeral_agent(kiro.name)["state"] == "pending"
    kiro.backend.create_window.assert_not_called()
    kiro.backend.create_session.assert_not_called()
    kiro.factory.assert_not_called()
    assert not os.path.lexists(home())
    assert database.get_terminal_metadata(CHILD) is None


def assert_refused_before_the_probe(kiro):
    assert_refused_before_bind(kiro)
    kiro.probe.assert_not_called()
    assert kiro.calls() == []


def assert_launch_failed(kiro):
    row = database.get_ephemeral_agent(kiro.name)
    assert row["state"] == "gc" and row["gc_reason"] == "launch_failed"
    assert row["launched_terminal_id"] == CHILD
    assert database.get_terminal_metadata(CHILD) is None
    kiro.backend.create_window.assert_not_called()
    kiro.factory.assert_not_called()


async def policy_rule(kiro, **kwargs) -> str:
    with pytest.raises(kiro.service.EphemeralPolicyError) as err:
        await launch(kiro, **kwargs)
    assert err.value.status_code == 400
    return err.value.rule


def kiro_events(kiro):
    return {e["event"]: e for e in audit(kiro.name)["events"] if e["event"].startswith("kiro_")}


# --- the launch ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_helper_launches_on_v3_from_its_sealed_home_before_the_window(kiro):
    terminal = await launch(kiro)
    assert terminal.id == CHILD and terminal.ephemeral is True
    assert kiro.seen["home_existed"] is True
    assert kiro.seen["window_env"]["KIRO_HOME"] == str(home())
    assert "KIRO_HOME" not in session_env.get_session_env("cao-session")
    assert stat.S_IMODE(os.lstat(home() / "agents").st_mode) == 0o500
    assert stat.S_IMODE(os.lstat(home() / "powers").st_mode) == 0
    agent = json.loads((home() / "agents" / f"{kiro.name}.json").read_text())
    assert agent["allowedTools"] == ["@cao-mcp-server", "fs_list", "fs_read"]
    assert "permissions" in agent and "excludedTools" in agent
    assert set(agent["mcpServers"]) == {"cao-mcp-server"}
    assert agent["mcpServers"]["cao-mcp-server"]["env"]["CAO_TERMINAL_ID"] == CHILD

    kwargs = kiro.factory.call_args.kwargs
    assert kwargs["engine"] == KiroEngine.V3
    launch_object = kwargs["kiro_launch"]
    assert launch_object.home.home == home()
    assert launch_object.grant == ("@cao-mcp-server", "fs_list", "fs_read")
    assert launch_object.version == "2.35.1" and launch_object.schema
    assert database.get_terminal_metadata(CHILD)["engine"] == "v3"

    # One binary everywhere: the probe ran the kiro-cli the pane runs, under
    # the probe home, and help and the version are all it ran.
    assert launch_object.kiro_cli.path == str(kiro.script)
    assert kiro.probe.call_args.kwargs["executable"] == str(kiro.script)
    assert [call["argv"][1:] for call in kiro.calls()] == [
        ["--help"],
        ["chat", "--help"],
        ["--version"],
    ]
    assert {call["argv"][0] for call in kiro.calls()} == {str(kiro.script)}
    assert {call["home"] for call in kiro.calls()} == {str(kiro_launch_home.kiro_probe_home())}

    prepared = kiro_events(kiro)["kiro_home_prepared"]
    assert prepared["home"] == str(home()) and prepared["kiro_cli"] == str(kiro.script)
    assert set(prepared["hashes"]) == {
        "settings/cli.json",
        "agents/cao-null.json",
        f"agents/{kiro.name}.json",
        "secrets.json",
    }
    assert prepared["placeholder"]["mode"] == 0
    assert prepared["global_home"]["powers_marker"] is True
    assert prepared["kiro_version"] == "2.35.1"


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", [None, "v3", "kas", KiroEngine.V3])
async def test_no_engine_v3_and_kas_all_run_a_helper_on_v3(kiro, engine):
    await launch(kiro, engine=engine)
    assert kiro.factory.call_args.kwargs["engine"] == KiroEngine.V3


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["v2", KiroEngine.V2])
async def test_v2_refuses_a_helper_before_bind_and_returns_the_claim(kiro, engine):
    token = claim(kiro.env, kiro.name)["claim_id"]
    assert await policy_rule(kiro, engine=engine, claim_id=token) == "kiro_ephemeral_engine:v2"
    assert_refused_before_the_probe(kiro)
    assert audit(kiro.name)["refusals"]["kiro_ephemeral_engine:v2"]["count"] == 1


@pytest.mark.asyncio
async def test_a_kiro_cli_version_off_the_allow_list_refuses_before_bind(kiro):
    (kiro.fake / "version").write_text("9.9.9")
    with pytest.raises(KiroCapabilityError) as err:
        await launch(kiro)
    assert err.value.kind == "kiro_v3_unsupported_version"
    assert_refused_before_bind(kiro)


@pytest.mark.asyncio
async def test_a_pending_version_needs_the_operators_opt_in(kiro, monkeypatch):
    (kiro.fake / "version").write_text("2.36.0")
    assert await policy_rule(kiro) == "kiro_v3_unverified_version"
    assert_refused_before_bind(kiro)
    monkeypatch.setattr(kiro_v3_launch, "kiro_v3_allow_unverified_versions", lambda: True)
    terminal = await launch(kiro)
    assert terminal.id == CHILD
    assert kiro.factory.call_args.kwargs["kiro_launch"].version == "2.36.0"


# --- Kiro's legacy copies (stage A, before the probe) ------------------------------


@pytest.mark.asyncio
async def test_a_secret_store_without_its_marker_refuses_before_the_probe(kiro):
    kiro.operator.set_entry("secrets.json", "file")
    token = claim(kiro.env, kiro.name)["claim_id"]
    assert await policy_rule(kiro, claim_id=token) == "kiro_v3_global_home:secrets"
    assert_refused_before_the_probe(kiro)
    # Once Kiro has marked it migrated, its own guard skips the copy.
    kiro.operator.set_entry(".secrets-migrated", "file")
    assert (await launch(kiro)).id == CHILD


@pytest.mark.asyncio
async def test_a_listable_placeholder_refuses_only_when_kiro_would_copy_powers(kiro, monkeypatch):
    monkeypatch.setattr(kiro_launch_home, "placeholder_is_listable", lambda path: True)
    kiro.operator.set_entry(".powers-migrated", "absent")
    assert await policy_rule(kiro) == "kiro_v3_global_home:powers"
    assert_refused_before_the_probe(kiro)
    # With the marker (or no powers at all) Kiro's own guard skips the copy.
    kiro.operator.set_entry(".powers-migrated", "file")
    assert (await launch(kiro)).id == CHILD


@pytest.mark.asyncio
async def test_a_server_without_a_home_refuses_before_the_probe(kiro, monkeypatch):
    monkeypatch.delenv("HOME")
    assert await policy_rule(kiro) == "kiro_v3_global_home:unknown"
    assert_refused_before_the_probe(kiro)


@pytest.mark.asyncio
async def test_a_cao_home_kiro_would_ignore_refuses_before_the_probe(kiro, monkeypatch):
    monkeypatch.setenv("HOME", str(kiro_launch_home.kiro_probe_home() / "operator"))
    assert await policy_rule(kiro) == "kiro_home_invalid:ignored"
    assert_refused_before_the_probe(kiro)


# --- the other stage-A refusals ------------------------------------------------------


@pytest.mark.asyncio
async def test_no_kiro_cli_on_the_server_path_refuses_before_bind(kiro, monkeypatch):
    monkeypatch.setenv("PATH", str(kiro.repo))
    token = claim(kiro.env, kiro.name)["claim_id"]
    assert await policy_rule(kiro, claim_id=token) == "kiro_cli_unresolved"
    assert_refused_before_the_probe(kiro)


@pytest.mark.asyncio
async def test_a_forwarded_kiro_home_refuses_before_bind(kiro, tmp_path):
    rule = await policy_rule(kiro, env_vars={"KIRO_HOME": str(tmp_path / "mine")})
    assert rule == "kiro_home_invalid:forwarded"
    assert_refused_before_the_probe(kiro)
    assert audit(kiro.name)["refusals"]["kiro_home_invalid:forwarded"]["count"] == 1


@pytest.mark.asyncio
async def test_on_herdr_a_home_path_it_cannot_pass_refuses_before_bind(kiro, tmp_path, monkeypatch):
    from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend

    unsafe = tmp_path / "cao$home" / "ephemeral" / "kiro"
    unsafe.parent.parent.mkdir()
    monkeypatch.setattr(kiro_launch_home, "kiro_homes_root", lambda: unsafe)
    herdr = Mock(spec=HerdrBackend)
    monkeypatch.setattr(terminal_service, "get_backend", lambda: herdr)
    assert await policy_rule(kiro) == "kiro_home_invalid:unsafe_path"
    assert_refused_before_the_probe(kiro)
    herdr.create_session.assert_not_called()
    herdr.create_window.assert_not_called()

    # The same path is no reason to refuse on tmux.
    monkeypatch.setattr(terminal_service, "get_backend", lambda: kiro.backend)
    terminal = await launch(kiro)
    assert terminal.id == CHILD and kiro.seen["window_env"]["KIRO_HOME"] == str(home())


# --- what stage A hands on (the 2a review's observations) ----------------------


@pytest.mark.asyncio
async def test_a_kiro_cli_in_a_folder_holding_equals_refuses_before_the_probe(kiro, monkeypatch):
    """env would read ``…/fake=bin/kiro-cli`` as an assignment, so the probe and the pane would differ."""
    odd = kiro.fake.parent / "fake=bin"
    odd.mkdir()
    (odd / "kiro-cli").write_text(kiro.script.read_text())
    (odd / "kiro-cli").chmod(0o755)
    monkeypatch.setenv("PATH", f"{odd}{os.pathsep}{os.environ['PATH']}")
    assert await policy_rule(kiro) == "kiro_cli_unresolved"
    assert_refused_before_the_probe(kiro)


@pytest.mark.asyncio
async def test_the_workspace_checks_get_the_validated_home_and_come_after_it(kiro, monkeypatch):
    real = terminal_service.check_kiro_helper_workspace
    calls = []

    def spy(cwd, home):
        calls.append((cwd, home))
        return real(cwd, home)

    monkeypatch.setattr(terminal_service, "check_kiro_helper_workspace", spy)
    await launch(kiro)
    # A5 (before bind) and the final cwd (stage B), both with server_home()'s value.
    assert calls == [(str(kiro.repo), os.environ["HOME"])] * 2
    calls.clear()
    kiro_launch_home.discard_launch_home(CHILD)
    monkeypatch.delenv("HOME")
    kiro.name = create(kiro.env, {**SPEC, "purpose": "no_home", "provider": "kiro_cli"})["name"]
    assert await policy_rule(kiro) == "kiro_v3_global_home:unknown"
    assert calls == []  # H5's :unknown comes first


@pytest.mark.asyncio
async def test_the_home_is_created_for_the_bound_ephemeral_name(kiro, monkeypatch):
    real = kiro_launch_home.create_launch_home
    names = []

    def spy(terminal_id, agent, payload, global_home):
        names.append(agent)
        return real(terminal_id, agent, payload, global_home)

    monkeypatch.setattr(kiro_launch_home, "create_launch_home", spy)
    await launch(kiro)
    assert names == [kiro.name] and kiro_launch_home._plain_name(kiro.name)
    assert database.get_ephemeral_agent(kiro.name)["launched_terminal_id"] == CHILD


# --- the workspace ------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["agents", "hooks", "settings", "powers"])
async def test_workspace_configuration_refuses_before_bind(kiro, entry):
    (kiro.repo / ".kiro" / entry).mkdir(parents=True)
    assert await policy_rule(kiro) == f"kiro_v3_workspace_config:{entry}"
    assert_refused_before_bind(kiro)


@pytest.mark.asyncio
async def test_launching_from_home_refuses_before_bind(kiro):
    rule = await policy_rule(kiro, working_directory=str(kiro.operator.home))
    assert rule == "kiro_v3_workspace_config:home"
    assert_refused_before_bind(kiro)


@pytest.mark.asyncio
async def test_launching_under_a_home_that_is_a_git_work_tree_refuses_before_bind(kiro):
    repo = kiro.operator.home / "Project" / "repo"
    repo.mkdir(parents=True)
    (kiro.operator.home / ".git").mkdir()
    rule = await policy_rule(kiro, working_directory=str(repo))
    assert rule == "kiro_v3_workspace_config:home"
    assert_refused_before_bind(kiro)


@pytest.mark.asyncio
async def test_a_repository_under_home_launches_though_kiro_holds_configuration(kiro):
    # ~/.kiro holds agents/, settings/ and powers/; only stat reaches it.
    repo = kiro.operator.home / "Project" / "repo"
    repo.mkdir(parents=True)
    assert (await launch(kiro, working_directory=str(repo))).id == CHILD


@pytest.mark.asyncio
async def test_workspace_configuration_in_the_worktree_refuses_after_bind(
    kiro, tmp_path, monkeypatch
):
    worktree = tmp_path / "worktree"
    (worktree / ".kiro" / "agents").mkdir(parents=True)
    monkeypatch.setattr(
        terminal_service.worktree_service, "find_repo_root", lambda _: str(kiro.repo)
    )
    monkeypatch.setattr(
        terminal_service.worktree_service, "create_worktree", lambda root, tid: str(worktree)
    )
    removed = Mock()
    monkeypatch.setattr(terminal_service.worktree_service, "remove_worktree", removed)
    assert await policy_rule(kiro, use_worktree=True) == "kiro_v3_workspace_config:agents"
    assert_launch_failed(kiro)
    assert not os.path.lexists(home())
    removed.assert_called_once_with(str(kiro.repo), CHILD)


# --- stage B, after bind and before the window -------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["directory", "file", "symlink"])
async def test_an_entry_at_the_home_path_refuses_and_is_untouched(kiro, tmp_path, kind):
    home().parent.mkdir(parents=True, exist_ok=True)
    if kind == "directory":
        home().mkdir()
        (home() / "planted").write_text("x")
    elif kind == "file":
        home().write_text("planted")
    else:
        home().symlink_to(tmp_path)
    before = os.lstat(home()).st_ino
    assert await policy_rule(kiro) == "kiro_home_invalid:exists"
    assert os.lstat(home()).st_ino == before
    if kind == "directory":
        assert (home() / "planted").read_text() == "x"
    assert_launch_failed(kiro)


@pytest.mark.asyncio
async def test_a_helper_file_the_validator_refuses_never_reaches_a_window(kiro, monkeypatch):
    real = kiro_launch_home.render_helper_agent

    def wider(name, profile, grant, terminal_id):
        data = json.loads(real(name, profile, grant, terminal_id))
        data["tools"].append("execute_bash")
        return (json.dumps(data, indent=2) + "\n").encode()

    monkeypatch.setattr(kiro_launch_home, "render_helper_agent", wider)
    assert (await policy_rule(kiro)).startswith("kiro_v3_invalid:")
    assert_launch_failed(kiro)
    assert not os.path.lexists(home())


@pytest.mark.asyncio
async def test_a_restricted_creator_never_reads_the_installed_store(kiro, monkeypatch):
    installed = Mock(return_value=None)
    monkeypatch.setattr(terminal_service, "installed_kiro_tools", installed)
    predates = Mock(return_value=True)
    monkeypatch.setattr(terminal_service, "kiro_install_predates_native_enforcement", predates)
    launchable = Mock()
    monkeypatch.setattr(terminal_service, "require_kiro_v3_launchable", launchable)
    resolves = Mock()
    monkeypatch.setattr(terminal_service, "require_kiro_agent_resolves", resolves)
    await launch(kiro, allowed_tools=["fs_read", "@cao-mcp-server"])
    for check in (installed, predates, launchable, resolves):
        check.assert_not_called()
    agent = json.loads((home() / "agents" / f"{kiro.name}.json").read_text())
    assert agent["allowedTools"] == ["@cao-mcp-server", "fs_read"]


@pytest.mark.asyncio
async def test_a_refusal_after_the_window_rolls_back_the_home(kiro):
    kiro.provider.initialize.side_effect = KiroLaunchRefusal("kiro_v3_global_home:changed")
    with pytest.raises(KiroLaunchRefusal):
        await launch(kiro)
    row = database.get_ephemeral_agent(kiro.name)
    assert row["state"] == "gc" and row["gc_reason"] == "launch_failed"
    assert database.get_terminal_metadata(CHILD) is None
    assert not os.path.lexists(home())


@pytest.mark.asyncio
async def test_a_cancel_before_any_provider_owns_the_home_removes_it(kiro):
    kiro.factory.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await launch(kiro)
    row = database.get_ephemeral_agent(kiro.name)
    assert row["state"] == "gc" and row["gc_reason"] == "launch_failed"
    assert not os.path.lexists(home())


@pytest.mark.asyncio
async def test_a_deferred_kiro_refusal_keeps_its_rule(kiro, monkeypatch):
    surfaced = AsyncMock(return_value=True)
    monkeypatch.setattr(terminal_service, "_surface_deferred_init_failure", surfaced)
    for error, rule in (
        (KiroLaunchRefusal("kiro_v3_global_home:changed"), "kiro_v3_global_home:changed"),
        (kiro_launch_home.KiroV3Refusal("x"), "kiro_v3_refused"),
    ):
        provider = Mock(initialize=AsyncMock(side_effect=error))
        task = terminal_service._schedule_deferred_init(
            provider, CHILD, None, None, None, delete_on_failure=True, provider="kiro_cli"
        )
        await task
        assert surfaced.call_args.kwargs["rule"] == rule


def test_the_persisted_failure_carries_the_rule(kiro):
    database.create_terminal(CHILD, "cao-session", "child", "kiro_cli")
    failure = terminal_service._persist_deferred_init_failure(
        CHILD,
        kind="provider_init_error",
        message="refused",
        untrusted_runtime=True,
        rule="kiro_v3_tools_mismatch",
    )
    assert failure is not None and failure["rule"] == "kiro_v3_tools_mismatch"
    stored = database.get_terminal_metadata(CHILD)["deferred_init_failure"]
    assert stored["rule"] == "kiro_v3_tools_mismatch"


@pytest.mark.asyncio
async def test_a_tier_and_an_effort_are_probed_and_reach_the_launch(kiro):
    kiro.env[4]["model_tiers"] = {"kiro_cli": {"small": "claude-sonnet-5.5"}}
    name = create(
        kiro.env,
        {
            **SPEC,
            "purpose": "tiered_triage",
            "provider": "kiro_cli",
            "model_tier": "small",
            "effort": "medium",
        },
    )["name"]
    kiro.name = name
    await launch(kiro)
    requested = kiro.probe.call_args.args[1]
    assert {"model", "effort", "trust_workspace"} <= requested
    launch_object = kiro.factory.call_args.kwargs["kiro_launch"]
    assert launch_object.model == "claude-sonnet-5.5" and launch_object.effort == "medium"


@pytest.mark.asyncio
async def test_a_wrapper_without_effort_refuses_an_effort_launch_before_bind(kiro):
    spec = {**SPEC, "purpose": "effort_triage", "provider": "kiro_cli", "effort": "high"}
    kiro.name = create(kiro.env, spec)["name"]
    chat = (FIXTURES / "kiro_cli_2_35_1_chat_help.txt").read_text()
    stripped = "\n".join(line for line in chat.splitlines() if "--effort" not in line)
    (kiro.fake / "chat-help.txt").write_text(stripped)
    kiro.script.write_text(
        kiro.script.read_text().replace(
            repr(str(FIXTURES / "kiro_cli_2_35_1_chat_help.txt")),
            repr(str(kiro.fake / "chat-help.txt")),
        )
    )
    with pytest.raises(KiroCapabilityError) as err:
        await launch(kiro)
    assert err.value.capability == "--effort"
    assert_refused_before_bind(kiro)


# --- the session environment ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_window_in_an_existing_session_never_touches_its_environment(kiro):
    await launch(kiro)
    kiro.backend.restore_session_env.assert_not_called()


@pytest.mark.asyncio
async def test_a_new_session_never_persists_the_home(kiro):
    kiro.backend.session_exists.return_value = False
    await launch(kiro, session_name="cao-fresh", new_session=True, env_vars={"MARK": "1"})
    assert kiro.seen["window_env"]["KIRO_HOME"] == str(home())
    assert session_env.get_session_env("cao-fresh") == {"MARK": "1"}
    kiro.backend.restore_session_env.assert_called_once_with("cao-fresh", "KIRO_HOME", str(home()))


@pytest.mark.asyncio
async def test_a_new_session_that_keeps_the_home_is_torn_down_and_the_launch_fails(kiro):
    from cli_agent_orchestrator.backends.base import TerminalBackendError

    kiro.backend.session_exists.return_value = False
    kiro.backend.restore_session_env.side_effect = TerminalBackendError("still there")
    with pytest.raises(TerminalBackendError):
        await launch(kiro, session_name="cao-fresh", new_session=True)
    kiro.backend.kill_session.assert_called_once_with("cao-fresh")
    row = database.get_ephemeral_agent(kiro.name)
    assert row["state"] == "gc" and row["gc_reason"] == "launch_failed"
    assert database.get_terminal_metadata(CHILD) is None
    assert not os.path.lexists(home())
    kiro.factory.assert_not_called()


_PANE_ENV_PROBE = """import json, os, sys
keys = ("KIRO_HOME", "CAO_TERMINAL_ID")
with open(sys.argv[1] + ".tmp", "w") as out:
    json.dump({key: os.environ.get(key) for key in keys}, out)
os.replace(sys.argv[1] + ".tmp", sys.argv[1])
"""


@pytest.fixture
def private_tmux(kiro, monkeypatch):
    """A real TmuxBackend on a private tmux server, under a scratch HOME and TMUX_TMPDIR.

    Its default socket, so CAO's plain ``tmux`` calls reach this server too;
    /bin/sh panes, so no rc file of the user's runs. The server is killed by
    its own socket, and the scratch directory removed by its exact path.
    """
    if shutil.which("tmux") is None:
        pytest.skip("tmux is not installed")
    import libtmux

    from cli_agent_orchestrator.backends.tmux_backend import TmuxBackend
    from cli_agent_orchestrator.clients.tmux import TmuxClient

    # Short: pytest's tmp_path can exceed the AF_UNIX socket path limit on macOS.
    scratch = Path(tempfile.mkdtemp(prefix="cao-p2-", dir="/tmp"))
    (scratch / "tmux").mkdir(mode=0o700)
    (scratch / "home").mkdir()
    config = scratch / "tmux.conf"
    config.write_text("set -g default-shell /bin/sh\n")
    (scratch / "probe.py").write_text(_PANE_ENV_PROBE)
    monkeypatch.setenv("TMUX_TMPDIR", str(scratch / "tmux"))
    monkeypatch.setenv("HOME", str(scratch / "home"))
    monkeypatch.setenv("SHELL", "/bin/sh")
    for name in ("TMUX", "TMUX_PANE", "KIRO_HOME"):
        monkeypatch.delenv(name, raising=False)
    client = TmuxClient()
    client.server = libtmux.Server(config_file=str(config))
    backend = TmuxBackend(client=client)
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    socket = scratch / "tmux" / f"tmux-{os.getuid()}" / "default"

    def tmux(*args):
        return subprocess.run(
            ["tmux", "-S", str(socket), *args], capture_output=True, text=True, check=True
        ).stdout

    def pane_env(target, label):
        """Run the probe in the pane's own shell; the env its processes start with."""
        out = scratch / f"{label}.json"
        command = f"{shlex.quote(sys.executable)} {shlex.quote(str(scratch / 'probe.py'))} {out}"
        tmux("send-keys", "-t", target, command, "Enter")
        deadline = time.monotonic() + 20
        while not out.exists():
            assert time.monotonic() < deadline, f"no probe output from {target}"
            time.sleep(0.05)
        return json.loads(out.read_text())

    try:
        yield SimpleEnv(backend=backend, tmux=tmux, pane_env=pane_env)
    finally:
        subprocess.run(["tmux", "-S", str(socket), "kill-server"], capture_output=True, check=False)
        shutil.rmtree(scratch, ignore_errors=True)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_new_tmux_session_keeps_the_home_out_of_its_environment(kiro, private_tmux):
    """Real tmux: only the first window has the helper's KIRO_HOME (#1167 Phase 2)."""
    terminal = await launch(
        kiro, session_name="cao-fresh", new_session=True, env_vars={"MARK": "1"}
    )
    assert terminal.id == CHILD

    session_env_lines = private_tmux.tmux("show-environment", "-t", "cao-fresh").splitlines()
    # The read is real: the forwarded env and the terminal id are session state.
    assert "MARK=1" in session_env_lines and f"CAO_TERMINAL_ID={CHILD}" in session_env_lines
    assert not [line for line in session_env_lines if line.lstrip("-").startswith("KIRO_HOME")]

    first = private_tmux.pane_env(f"cao-fresh:{terminal.name}", "first")
    assert first == {"KIRO_HOME": str(home()), "CAO_TERMINAL_ID": CHILD}

    # A later window, created the way create_terminal adds one to the session.
    window = private_tmux.backend.create_window(
        "cao-fresh",
        "sibling",
        "bbbbbbbb",
        str(kiro.repo),
        extra_env=session_env.get_session_env("cao-fresh"),
    )
    later = private_tmux.pane_env(f"cao-fresh:{window}", "later")
    assert later == {"KIRO_HOME": None, "CAO_TERMINAL_ID": "bbbbbbbb"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_new_tmux_session_gives_later_windows_the_servers_own_home(
    kiro, private_tmux, monkeypatch, tmp_path
):
    """Real tmux: later windows keep the server's KIRO_HOME, never the helper's (#1167)."""
    # The tmux server starts without KIRO_HOME, as an operator's server started
    # apart from CAO would; then the CAO process gets its own.
    private_tmux.backend.create_session("keep", "w", "cccccccc", str(kiro.repo))
    global_env = private_tmux.tmux("show-environment", "-g").splitlines()
    assert not [line for line in global_env if line.lstrip("-").startswith("KIRO_HOME")]
    server_home = tmp_path / "server-kiro"
    server_home.mkdir()
    monkeypatch.setenv("KIRO_HOME", str(server_home))

    # Control: a later window of any other new session gets the server's home.
    private_tmux.backend.create_session("ctl", "w", "dddddddd", str(kiro.repo))
    window = private_tmux.backend.create_window("ctl", "sib", "dddddd01", str(kiro.repo))
    assert private_tmux.pane_env(f"ctl:{window}", "control")["KIRO_HOME"] == str(server_home)

    terminal = await launch(kiro, session_name="cao-fresh", new_session=True)
    session_env_lines = private_tmux.tmux("show-environment", "-t", "cao-fresh").splitlines()
    assert f"KIRO_HOME={server_home}" in session_env_lines
    assert not [line for line in session_env_lines if str(home()) in line]
    first = private_tmux.pane_env(f"cao-fresh:{terminal.name}", "first")
    assert first["KIRO_HOME"] == str(home())

    window = private_tmux.backend.create_window(
        "cao-fresh",
        "sibling",
        "bbbbbbbb",
        str(kiro.repo),
        extra_env=session_env.get_session_env("cao-fresh"),
    )
    later = private_tmux.pane_env(f"cao-fresh:{window}", "later")
    assert later == {"KIRO_HOME": str(server_home), "CAO_TERMINAL_ID": "bbbbbbbb"}


@pytest.fixture
def private_herdr(kiro, monkeypatch):
    """A real HerdrBackend on a private herdr server, under a scratch HOME.

    Every HERDR_* variable is removed first, so no call can reach another
    server. The server is started here, so its PID is known: it is stopped
    through its own socket, then by that PID, and the scratch directory is
    removed by its exact path.
    """
    if shutil.which("herdr") is None:
        pytest.skip("herdr is not installed")
    from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend

    scratch = Path(tempfile.mkdtemp(prefix="cao-p2h-", dir="/tmp"))
    config = scratch / "home" / ".config" / "herdr"
    config.mkdir(parents=True)
    (config / "config.toml").write_text(
        'default_shell = "/bin/sh"\n[update]\nversion_check = false\nmanifest_check = false\n'
    )
    (scratch / "probe.py").write_text(_PANE_ENV_PROBE)
    for name in [name for name in os.environ if name.startswith(("HERDR_", "XDG_"))]:
        monkeypatch.delenv(name)
    for name in ("TMUX", "TMUX_PANE", "KIRO_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(scratch / "home"))
    monkeypatch.setenv("SHELL", "/bin/sh")
    session = "p2"
    socket = config / "sessions" / session / "herdr.sock"
    server = subprocess.Popen(
        ["herdr", "--session", session, "server"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def herdr(*args):
        return subprocess.run(
            ["herdr", "--session", session, *args], capture_output=True, text=True, check=True
        ).stdout

    def pane_env(terminal_id, label):
        out = scratch / f"{label}.json"
        command = f"{shlex.quote(sys.executable)} {shlex.quote(str(scratch / 'probe.py'))} {out}"
        herdr("pane", "run", backend.get_pane_id(terminal_id), command)
        deadline = time.monotonic() + 20
        while not out.exists():
            assert time.monotonic() < deadline, f"no probe output from {terminal_id}"
            time.sleep(0.05)
        return json.loads(out.read_text())

    try:
        deadline = time.monotonic() + 20
        while not socket.exists():
            assert server.poll() is None and time.monotonic() < deadline, "herdr did not start"
            time.sleep(0.05)
        backend = HerdrBackend(herdr_session=session)
        monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
        yield SimpleEnv(backend=backend, pane_env=pane_env)
    finally:
        subprocess.run(
            ["herdr", "--session", session, "server", "stop"], capture_output=True, check=False
        )
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=10)
        shutil.rmtree(scratch, ignore_errors=True)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_on_herdr_only_the_launched_pane_has_the_home(kiro, private_herdr):
    """Real herdr (provisional, #1167): ``--env`` is scoped to the pane it launches.

    So herdr needs no session-env removal, and new-session Kiro helpers are not
    refused there. If a herdr release makes the workspace keep the value, this
    fails.
    """
    terminal = await launch(
        kiro, session_name="cao-fresh", new_session=True, env_vars={"MARK": "1"}
    )
    assert terminal.id == CHILD
    first = private_herdr.pane_env(CHILD, "first")
    assert first == {"KIRO_HOME": str(home()), "CAO_TERMINAL_ID": CHILD}

    private_herdr.backend.create_window(
        "cao-fresh",
        "sibling",
        "bbbbbbbb",
        str(kiro.repo),
        extra_env=session_env.get_session_env("cao-fresh"),
    )
    later = private_herdr.pane_env("bbbbbbbb", "later")
    assert later == {"KIRO_HOME": None, "CAO_TERMINAL_ID": "bbbbbbbb"}
