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
from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.providers import kiro_launch_home
from cli_agent_orchestrator.providers.kiro_capabilities import (
    MISSING_FLAG_REMEDY,
    KiroCapabilityError,
    KiroV3LaunchError,
    missing_flag_message,
    probe_kiro_capabilities,
)
from cli_agent_orchestrator.providers.kiro_launch_home import KiroLaunchRefusal
from cli_agent_orchestrator.providers.kiro_v3_schema import (
    UNSUPPORTED_VERSION_REMEDY,
    UNVERIFIED_VERSION_REMEDY,
    KiroV3UnsupportedVersionError,
    KiroV3UnverifiedVersionError,
)
from cli_agent_orchestrator.services import kiro_v3_launch, session_env, terminal_service

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
    # The helper cut is Claude Code's: a Kiro helper gets no ephemeral flag and its
    # bound event records no Claude Code deny list.
    assert "ephemeral" not in kwargs
    (bound,) = [e for e in audit(kiro.name)["events"] if e["event"] == "bound"]
    assert "disallowed_tools" not in bound
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
    with pytest.raises(kiro.service.EphemeralPolicyError) as err:
        await launch(kiro)
    assert err.value.rule == "kiro_v3_unsupported_version"
    assert "9.9.9" in err.value.detail
    assert audit(kiro.name)["refusals"][err.value.rule]["count"] == 1
    # A helper runs only on v3, so the remedy is a supported kiro-cli, never v2.
    assert "v2" not in str(err.value)
    assert str(err.value).endswith(terminal_service._HELPER_SUPPORTED_CLI)
    assert_refused_before_bind(kiro)


@pytest.mark.asyncio
async def test_a_pending_version_needs_the_operators_opt_in(kiro, monkeypatch):
    (kiro.fake / "version").write_text("2.36.0")
    with pytest.raises(kiro.service.EphemeralPolicyError) as err:
        await launch(kiro)
    assert err.value.rule == "kiro_v3_unverified_version"
    # The remedy is the opt-in or a verified kiro-cli, never v2, which a helper refuses.
    assert "v2" not in err.value.message
    assert err.value.detail.endswith(terminal_service._HELPER_VERIFIED_CLI)
    assert "kiro.v3_allow_unverified_versions" in err.value.detail
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
    user_home = str(kiro_launch_home.kiro_probe_home() / "operator")
    monkeypatch.setenv("HOME", user_home)
    with pytest.raises(kiro.service.EphemeralPolicyError) as err:
        await launch(kiro)
    assert err.value.rule == "kiro_home_invalid:ignored"
    # The cause and the fix, and no server path: this is the 400 body.
    detail = err.value.detail
    assert detail.endswith("so that HOME is not under $CAO_HOME_DIR/ephemeral/")
    for path in {str(kiro.cao_home), os.path.realpath(kiro.cao_home), user_home}:
        assert path not in detail
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
async def test_a_planned_effort_reaches_the_launch_before_kiro_config(kiro):
    """A launch plan's effort wins over the live profile's kiroConfig.effort, as for every
    provider, so nothing between the plan and --effort can drop it."""
    from cli_agent_orchestrator.decisions.types import LaunchPlan

    spec = {**SPEC, "purpose": "planned_triage", "provider": "kiro_cli", "effort": "medium"}
    kiro.name = create(kiro.env, spec)["name"]
    claimed = claim(kiro.env, kiro.name)["claim_id"]
    await launch(kiro, claim_id=claimed, launch_plan=LaunchPlan(effort="high"))
    assert "effort" in kiro.probe.call_args.args[1]
    assert kiro.factory.call_args.kwargs["kiro_launch"].effort == "high"


@pytest.mark.asyncio
async def test_a_planned_effort_alone_is_probed_and_reaches_the_launch(kiro):
    """With no stored effort, only the plan carries one: the probe must still ask for
    --effort, so the wrapper is never launched with a flag it was not probed for."""
    from cli_agent_orchestrator.decisions.types import LaunchPlan

    spec = {**SPEC, "purpose": "plan_only_triage", "provider": "kiro_cli"}
    created = create(kiro.env, spec)
    assert created["effort"] is None
    kiro.name = created["name"]
    claimed = claim(kiro.env, kiro.name)["claim_id"]
    await launch(kiro, claim_id=claimed, launch_plan=LaunchPlan(effort="high"))
    assert "effort" in kiro.probe.call_args.args[1]
    assert kiro.factory.call_args.kwargs["kiro_launch"].effort == "high"


# --- effort "auto": effort.route decides a helper's effort ------------------------


class EffortDecider:
    """Answers effort.route from a script; ``failure`` makes each ask fail that way."""

    name = "kiro_effort_test"
    version = "1"
    points = frozenset({"effort.route"})

    def __init__(self):
        self.answer, self.confidence, self.failure = "high", 0.91, None
        self.requests: list = []

    async def decide(self, request, config):
        from cli_agent_orchestrator.decisions.types import DecisionAnswer

        self.requests.append(request)
        if self.failure == "error":
            raise ValueError("synthetic decider failure")
        if self.failure == "timeout":
            await asyncio.sleep(1)
        return {
            point: DecisionAnswer(self.answer, {self.answer: self.confidence})
            for point in request.points
        }


@pytest.fixture
def decided(kiro, tmp_path, monkeypatch):
    """A Kiro helper declaring effort "auto", and the server's own decision path for it:
    decision_inputs, the engine with each provider's own honors_effort, the pre-bind
    recheck (``main._prepare_delegation``), then the launch with the plan it returns."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from cli_agent_orchestrator.api import main
    from cli_agent_orchestrator.decisions.engine import DecisionEngine, DelegationRequest
    from cli_agent_orchestrator.decisions.registry import DeciderRegistry
    from cli_agent_orchestrator.decisions.settings import DecisionSettings, PointSettings
    from cli_agent_orchestrator.decisions.shadow import ShadowRunner
    from cli_agent_orchestrator.decisions.store import DecisionStore
    from cli_agent_orchestrator.decisions.types import PointState

    kiro.env[4]["ephemeral"].update(min_effort="low", default_effort="medium", max_effort="high")
    spec = {**SPEC, "purpose": "auto_effort_triage", "provider": "kiro_cli", "effort": "auto"}
    created = create(kiro.env, spec)
    kiro.name = created["name"]
    db = create_engine(
        "sqlite:///" + str(tmp_path / "decisions.db"),
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    database.DecisionRecordModel.__table__.create(db)
    store = DecisionStore(
        sessionmaker(bind=db), tmp_path / "decision-hash.key", emit=lambda _: None
    )
    decider = EffortDecider()
    timeout = {"ms": 1000}
    engine = DecisionEngine(
        DeciderRegistry({decider.name: lambda: decider}),
        store,
        ShadowRunner(store),
        settings_loader=lambda: DecisionSettings(
            points={"effort.route": PointSettings(PointState.ON, decider.name)},
            on_timeout_ms=timeout["ms"],
        ),
    )
    monkeypatch.setattr(main.app.state, "decision_engine", engine, raising=False)

    async def run(**kwargs):
        claimed = claim(kiro.env, kiro.name)["claim_id"]
        request = DelegationRequest("handoff", "kiro_cli", kiro.name, None, "inspect")
        plan = await main._prepare_delegation(request, claimed, CALLER)
        await launch(kiro, claim_id=claimed, launch_plan=plan, **kwargs)
        return plan

    yield SimpleEnv(created=created, decider=decider, store=store, timeout=timeout, run=run)
    db.dispose()


def effort_record(decided):
    (record,) = decided.store.list()
    assert record["point"] == "effort.route" and record["provider"] == "kiro_cli"
    assert record["target_kind"] == "ephemeral" and record["fallback_value"] == "medium"
    return record


def launched_argv(kiro):
    """The helper's v3 command, built by its provider from the launch object it got."""
    from cli_agent_orchestrator.providers.kiro_capabilities import build_kiro_command
    from cli_agent_orchestrator.providers.kiro_cli import KiroCliProvider

    launch_object = kiro.factory.call_args.kwargs["kiro_launch"]
    provider = KiroCliProvider(
        CHILD, "cao-session", "child", kiro.name, engine=KiroEngine.V3, launch=launch_object
    )
    return build_kiro_command(
        KiroEngine.V3,
        kiro.name,
        model=provider._get_profile_model(),
        yolo=True,
        effort=provider._get_profile_effort(),
        **provider._launch_command_options(),
    )


def launched_effort(kiro):
    argv = launched_argv(kiro)
    assert argv.count("--effort") == 1
    return argv[argv.index("--effort") + 1]


@pytest.mark.asyncio
async def test_an_auto_effort_helper_runs_the_effort_effort_route_decides(kiro, decided):
    """A Kiro helper's effort "auto" asks effort.route at launch, and the decided level
    reaches kiro-cli as --effort through the launch plan, ahead of the stored effort."""
    assert decided.created["effort"] == "medium"
    assert decided.created["notes"][-1] == (
        "effort: auto (decided at launch; fallback effort medium, ephemeral.default_effort)"
    )
    plan = await decided.run()
    assert [request.points for request in decided.decider.requests] == [("effort.route",)]
    assert decided.decider.requests[0].facts.provider == "kiro_cli"
    assert plan.effort == "high"
    assert "effort" in kiro.probe.call_args.args[1]
    assert kiro.factory.call_args.kwargs["kiro_launch"].effort == "high"
    assert launched_effort(kiro) == "high"
    record = effort_record(decided)
    assert record["outcome"] == "applied" and record["reason"] is None
    assert record["answer"] == record["applied_value"] == "high"
    assert record["launch_status"] == "launched" and record["agent_profile"] == kiro.name
    # The decided effort applies to this launch only: the row keeps the stored effort.
    assert database.get_ephemeral_agent(kiro.name)["effort"] == "medium"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer,confidence,failure,reason",
    [
        ("unsure", 0.91, None, "unsure"),
        ("high", 0.2, None, "low_confidence"),
        ("high", 0.91, "timeout", "timeout"),
        ("high", 0.91, "error", "error"),
    ],
)
async def test_a_failed_effort_decision_runs_the_stored_effort_and_says_why(
    kiro, decided, answer, confidence, failure, reason
):
    decided.decider.answer, decided.decider.confidence = answer, confidence
    decided.decider.failure = failure
    decided.timeout["ms"] = 50
    plan = await decided.run()
    assert len(decided.decider.requests) == 1
    assert plan.effort == "medium"
    assert kiro.factory.call_args.kwargs["kiro_launch"].effort == "medium"
    assert launched_effort(kiro) == "medium"
    record = effort_record(decided)
    assert record["outcome"] == "fallback" and record["reason"] == reason
    assert record["applied_value"] is None and record["launch_status"] == "launched"


@pytest.mark.asyncio
async def test_a_decided_level_kiro_cli_rejects_fails_the_launch_without_a_retry(kiro, decided):
    """kiro-cli checks the level against the model. If it rejects the decided level, the
    start fails like any other: the launch is not retried on the stored effort."""
    kiro.provider.initialize.side_effect = TimeoutError("Kiro CLI initialization timed out")
    with pytest.raises(TimeoutError):
        await decided.run()
    assert kiro.factory.call_count == 1
    assert kiro.factory.call_args.kwargs["kiro_launch"].effort == "high"
    row = database.get_ephemeral_agent(kiro.name)
    assert row["state"] == "gc" and row["gc_reason"] == "launch_failed"
    assert not os.path.lexists(home())
    record = effort_record(decided)
    assert record["outcome"] == "applied" and record["launch_status"] == "launch_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["v2", KiroEngine.V2])
async def test_v2_still_refuses_a_helper_with_a_decided_effort_before_bind(kiro, decided, engine):
    with pytest.raises(kiro.service.EphemeralPolicyError) as err:
        await decided.run(engine=engine)
    assert err.value.rule == "kiro_ephemeral_engine:v2"
    assert_refused_before_the_probe(kiro)
    # Bound as every create that raises is, so no record of the decision stays open.
    assert effort_record(decided)["launch_status"] == "launch_failed"


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
    with pytest.raises(kiro.service.EphemeralPolicyError) as err:
        await launch(kiro)
    assert err.value.rule == "unsupported_capability" and "--effort" in err.value.detail
    assert audit(kiro.name)["refusals"][err.value.rule]["count"] == 1
    assert "v2" not in str(err.value)
    assert str(err.value).endswith(terminal_service._HELPER_SUPPORTED_CLI)
    assert_refused_before_bind(kiro)


def test_a_helpers_remedy_replaces_only_the_v2_clause():
    """Each helper refusal is Phase 1's, same code and fields, with the remedy swapped."""
    installed = KiroCapabilityError(
        "unsupported_capability",
        KiroEngine.V3,
        missing_flag_message(KiroEngine.V3, "--effort", "2.35.1", MISSING_FLAG_REMEDY),
        capability="--effort",
        version="2.35.1",
    )
    helper = terminal_service._helper_capability_error(installed)
    assert (helper.kind, helper.engine, helper.capability, helper.version) == (
        "unsupported_capability",
        KiroEngine.V3,
        "--effort",
        "2.35.1",
    )
    stem = str(installed).removesuffix(MISSING_FLAG_REMEDY)
    assert stem != str(installed) and str(helper) == stem + terminal_service._HELPER_SUPPORTED_CLI
    unsupported = KiroV3UnsupportedVersionError("9.9.9")
    for error, remedy in [
        (KiroV3UnverifiedVersionError("2.36.0"), UNVERIFIED_VERSION_REMEDY),
        (unsupported, UNSUPPORTED_VERSION_REMEDY),
    ]:
        swapped = terminal_service._helper_v3_refusal(error)
        assert type(swapped) is type(error) and swapped.code == error.code
        assert swapped.version == error.version and "v2" not in str(swapped)
        assert str(swapped).startswith(str(error).removesuffix(remedy))
    probed = KiroCapabilityError(
        unsupported.code, KiroEngine.V3, str(unsupported), capability="--version", version="9.9.9"
    )
    assert str(terminal_service._helper_capability_error(probed)) == str(
        terminal_service._helper_v3_refusal(unsupported)
    )
    # Refusals that suggest no engine are returned as they are.
    other = KiroCapabilityError("probe_failed", KiroEngine.V3, "kiro-cli --help failed")
    assert terminal_service._helper_capability_error(other) is other
    # The same kind, with a capability, but not the missing-flag text: left as it is.
    engine_value = KiroCapabilityError(
        "unsupported_capability",
        KiroEngine.V3,
        "Kiro engine 'v3' requires '--agent-engine' to accept 'v3', but the wrapper did not "
        "advertise that value. Detected wrapper version: 2.35.1.",
        capability="--agent-engine=v3",
        version="2.35.1",
    )
    assert terminal_service._helper_capability_error(engine_value) is engine_value
    unrestricted = KiroV3LaunchError("kiro_v3_unrestricted", "restrict the grant")
    assert terminal_service._helper_v3_refusal(unrestricted) is unrestricted


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


# --- release ----------------------------------------------------------------------


@pytest.fixture
def real_provider(kiro, monkeypatch):
    """The real KiroCliProvider in the real manager map; its initialize is a stand-in."""
    from cli_agent_orchestrator.providers.kiro_cli import KiroAttestation, KiroCliProvider
    from cli_agent_orchestrator.providers.manager import ProviderManager

    manager = terminal_service.provider_manager
    kiro.factory.side_effect = lambda *a, **kw: ProviderManager.create_provider(manager, *a, **kw)

    async def initialize(self):
        self.identity_verified = True
        self.attestation = KiroAttestation(("read",), (("cao-mcp-server", 34),), "auto")
        return True

    monkeypatch.setattr(KiroCliProvider, "initialize", initialize)
    owners = SimpleEnv(stoppable=True, calls=[])

    def stop(home):
        owners.calls.append(home)
        return owners.stoppable

    monkeypatch.setattr(kiro_launch_home, "stop_kiro_home_processes", stop)
    kiro.owners = owners
    yield kiro
    manager._providers.pop(CHILD, None)


@pytest.mark.asyncio
async def test_the_launch_is_audited_and_delete_removes_the_home_after_its_owners(real_provider):
    kiro = real_provider
    await launch(kiro)
    attested = kiro_events(kiro)["kiro_attested"]
    assert attested["tags"] == ["read"] and attested["mcp_tools"] == {"cao-mcp-server": 34}
    assert attested["kiro_version"] == "2.35.1" and attested["schema"]
    assert terminal_service.delete_terminal(CHILD, end_reason="deleted") is True
    assert kiro.owners.calls == [home()]
    assert not os.path.lexists(home())
    removed = kiro_events(kiro)["kiro_home_removed"]
    assert removed["agent_modified"] is False and removed["settings_modified"] is False
    assert removed["secrets_store_changed"] is False and removed["global_home_changed"] is False
    assert database.get_ephemeral_agent(kiro.name)["gc_reason"] == "terminal_deleted"
    # The launch cwd is untouched: the home lives under the CAO home only.
    assert list(kiro.repo.iterdir()) == []


@pytest.mark.asyncio
async def test_an_owner_that_may_remain_keeps_the_row_and_home_for_a_retry(real_provider):
    kiro = real_provider
    await launch(kiro)
    kiro.owners.stoppable = False
    assert terminal_service.delete_terminal(CHILD, end_reason="deleted") is False
    assert home().is_dir() and database.get_terminal_metadata(CHILD) is not None
    kiro.owners.stoppable = True
    assert terminal_service.delete_terminal(CHILD, end_reason="deleted") is True
    assert not os.path.lexists(home()) and database.get_terminal_metadata(CHILD) is None


@pytest.mark.asyncio
async def test_a_provider_rebuilt_after_a_restart_removes_the_home_on_delete(real_provider):
    kiro = real_provider
    await launch(kiro)
    manager = terminal_service.provider_manager
    manager._providers.pop(CHILD)
    kiro_launch_home.discard_launch_home(CHILD)
    rebuilt = manager.get_provider(CHILD)  # a status read after the restart rebuilds it
    assert rebuilt is not None and getattr(rebuilt, "_launch", None) is None
    assert terminal_service.delete_terminal(CHILD, end_reason="deleted") is True
    assert not os.path.lexists(home())
    assert kiro_events(kiro)["kiro_home_removed"]["agent_modified"] is False


@pytest.mark.asyncio
async def test_after_a_restart_delete_still_removes_the_home(real_provider):
    kiro = real_provider
    await launch(kiro)
    terminal_service.provider_manager._providers.pop(CHILD)
    kiro_launch_home.discard_launch_home(CHILD)
    kiro.operator.set_entry(".secrets-migrated", "file")  # Kiro wrote a marker meanwhile
    assert terminal_service.delete_terminal(CHILD, end_reason="deleted") is True
    assert not os.path.lexists(home())
    removed = kiro_events(kiro)["kiro_home_removed"]
    assert removed["agent_modified"] is False and removed["global_home_changed"] is True


# --- docs -------------------------------------------------------------------------


def test_every_helper_refusal_code_is_in_the_kiro_docs():
    """G26: each rule the helper path can raise has a row in the symptom table."""
    import re

    root = Path(__file__).parents[2]
    sources = [
        root / "src/cli_agent_orchestrator/providers/kiro_capabilities.py",
        root / "src/cli_agent_orchestrator/providers/kiro_launch_home.py",
        root / "src/cli_agent_orchestrator/providers/kiro_workspace.py",
        root / "src/cli_agent_orchestrator/services/terminal_service.py",
    ]
    codes = set()
    for source in sources:
        text = source.read_text(encoding="utf-8")
        codes |= set(re.findall(r'KiroLaunchRefusal\(\s*"([a-z0-9_:]+)"', text))
        codes |= set(re.findall(r'KiroCapabilityError\(\s*"([a-z0-9_:]+)"', text))
        codes |= set(re.findall(r'self\.code = "(kiro_[a-z0-9_:]+)"', text))
    assert {"kiro_v3_global_home:secrets", "kiro_ephemeral_engine:v2"} <= codes
    # The version refusals carry the helper's own remedy, so they get helper rows too.
    codes |= {KiroV3UnverifiedVersionError.code, KiroV3UnsupportedVersionError.code}
    docs = (root / "docs/kiro-cli.md").read_text(encoding="utf-8")
    table = docs[docs.index("#### When a helper is refused") :]
    table = table[: table.index("#### FAQ")]
    rows = "\n".join(line for line in table.splitlines() if line.startswith("| `"))
    missing = sorted(
        code
        for code in codes
        if f"`{code}`" not in rows and f"`:{code.split(':', 1)[-1]}`" not in rows
    )
    assert not missing, f"no symptom-table row for {missing}"
    fixes = [line.rstrip(" |").rsplit("|", 1)[-1] for line in rows.splitlines()]
    assert not [fix for fix in fixes if "v2" in fix], "a helper fix suggests engine v2"


@pytest.mark.asyncio
@pytest.mark.parametrize("final_failure", [False, True])
async def test_helper_decided_model_probe_falls_back_before_reporting(
    kiro, monkeypatch, final_failure
):
    from cli_agent_orchestrator.decisions import engine
    from cli_agent_orchestrator.decisions.types import LaunchPlan

    bound = {}
    monkeypatch.setattr(engine, "bind_launch", lambda plan, **values: bound.update(values))
    script = kiro.script.read_text()
    kiro.script.write_text(
        script.replace(
            'encoding="utf-8").read())',
            'encoding="utf-8").read().replace("--model", "--absent"))',
        )
    )
    if final_failure:
        (kiro.fake / "version").write_text("3.1.0")
        assert (
            await policy_rule(kiro, launch_plan=LaunchPlan(model="decided-model"))
            == "kiro_v3_unsupported_version"
        )
        assert audit(kiro.name)["refusals"]["kiro_v3_unsupported_version"]["count"] == 1
        assert "unsupported_capability" not in audit(kiro.name)["refusals"]
        assert_refused_before_bind(kiro)
    else:
        terminal = await launch(kiro, launch_plan=LaunchPlan(model="decided-model"))
        assert terminal.id == CHILD
        assert bound["not_honored"] is True
        assert kiro.factory.call_args.kwargs["kiro_launch"].model is None
        assert not audit(kiro.name).get("refusals", {})
    assert kiro.probe.call_count == 2
    assert "model" in kiro.probe.call_args_list[0].args[1]
    assert "model" not in kiro.probe.call_args_list[1].args[1]


def test_kiro_docs_guard_rejects_new_undocumented_capability_kind(monkeypatch):
    read_text = Path.read_text

    def undocumented_kind(path, *args, **kwargs):
        text = read_text(path, *args, **kwargs)
        if path.name == "kiro_capabilities.py":
            text += '\nraise KiroCapabilityError("new_undocumented_kind", engine, "refused")\n'
        return text

    monkeypatch.setattr(Path, "read_text", undocumented_kind)
    with pytest.raises(AssertionError, match="new_undocumented_kind"):
        test_every_helper_refusal_code_is_in_the_kiro_docs()
