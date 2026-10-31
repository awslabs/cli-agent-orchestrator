"""The pre-allocation checks every Kiro v3 create passes (#1167 Phase 1, gates G10, G12).

Each refusal comes after the capability probe and before any window, registry
row, FIFO or provider. A create that passes every check reaches allocation.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.kiro_agent import KiroAgentConfig
from cli_agent_orchestrator.providers.kiro_capabilities import KiroCapabilities, KiroV3LaunchError
from cli_agent_orchestrator.providers.kiro_v3_schema import (
    KAS_0_66_70,
    KiroV3Refusal,
    KiroV3UnsupportedVersionError,
    KiroV3UnverifiedVersionError,
    pinned_kiro_v3_schemas,
)
from cli_agent_orchestrator.providers.kiro_workspace import KiroWorkspaceError
from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.services.kiro_v3_launch import (
    require_default_kiro_home,
    require_kiro_v3_admissible,
    require_kiro_v3_launchable,
)
from cli_agent_orchestrator.services.kiro_v3_validate import KiroV3InvalidAgentError
from cli_agent_orchestrator.utils.tool_mapping import (
    kiro_universal_tools,
    kiro_v3_excluded_tools,
    kiro_v3_rules,
)

_TS = "cli_agent_orchestrator.services.terminal_service"
GRANT = ["fs_read", "fs_list", "@cao-mcp-server"]

pytestmark = pytest.mark.usefixtures("isolated_memory_db")


def _universal(name: str, grant=GRANT) -> str:
    config = KiroAgentConfig(
        name=name,
        description="d",
        tools=kiro_universal_tools(grant),
        allowedTools=list(grant),
        excludedTools=kiro_v3_excluded_tools(grant, pinned_kiro_v3_schemas()),
        mcpServers={"cao-mcp-server": {"command": "cao-mcp-server", "args": []}},
        permissions={"rules": kiro_v3_rules(grant, ["cao-mcp-server"])},
    )
    return config.model_dump_json(indent=2, exclude_none=True)


@pytest.fixture
def agents(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services import install_service

    directory = tmp_path / "kiro-agents"
    directory.mkdir()
    monkeypatch.setattr(install_service, "KIRO_AGENTS_DIR", directory)
    return directory


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    return path


def _caps(version="2.35.1"):
    return KiroCapabilities(version=version, flags=frozenset())


# --- the version and the policy ---------------------------------------------


_OPT_IN = "cli_agent_orchestrator.services.kiro_v3_launch.kiro_v3_allow_unverified_versions"


@pytest.fixture
def opted_in():
    with patch(_OPT_IN, return_value=True) as opt_in:
        yield opt_in


@pytest.fixture
def opted_out():
    with patch(_OPT_IN, return_value=False) as opt_in:
        yield opt_in


def test_a_measured_version_and_a_restricted_policy_are_admitted(opted_out):
    admission = require_kiro_v3_admissible(_caps(), GRANT)
    assert admission.schema is KAS_0_66_70
    assert admission.unverified_version is None and admission.launch_notes is None


@pytest.mark.parametrize(
    ("caps", "error"),
    [
        (_caps("2.36.0"), KiroV3UnverifiedVersionError),
        (_caps("2.37.0"), KiroV3UnverifiedVersionError),
        (_caps("2.38.0"), KiroV3UnsupportedVersionError),
        (_caps(None), KiroV3UnsupportedVersionError),
        (Mock(), KiroV3UnsupportedVersionError),
    ],
)
def test_versions_outside_the_admitted_list_refuse(opted_out, caps, error):
    """By default a pending version refuses, with its own code."""
    with pytest.raises(error) as excinfo:
        require_kiro_v3_admissible(caps, GRANT)
    assert excinfo.value.code == error.code


@pytest.mark.parametrize("version", ["2.36.0", "2.37.0"])
def test_the_opt_in_admits_a_pending_version_with_a_warning_and_a_note(opted_in, caplog, version):
    with caplog.at_level("WARNING", logger="cli_agent_orchestrator.services.kiro_v3_launch"):
        admission = require_kiro_v3_admissible(_caps(version), GRANT)
    assert admission.schema is KAS_0_66_70
    assert admission.unverified_version == version
    (note,) = admission.launch_notes
    assert note.startswith(f"kiro_v3_unverified_version_admitted: kiro-cli {version}")
    assert "pending operator verification" in note
    assert "kiro.v3_allow_unverified_versions" in note
    (record,) = [r for r in caplog.records if r.levelname == "WARNING"]
    assert version in record.getMessage()
    assert "kiro.v3_allow_unverified_versions" in record.getMessage()


@pytest.mark.parametrize("caps", [_caps("2.38.0"), _caps(None), _caps("v2.37.0"), Mock()])
def test_the_opt_in_never_admits_a_version_outside_the_allow_list(opted_in, caps):
    with pytest.raises(KiroV3UnsupportedVersionError):
        require_kiro_v3_admissible(caps, GRANT)


def test_the_opt_in_is_read_only_for_a_pending_version():
    with patch(_OPT_IN, side_effect=AssertionError("read for a non-pending version")):
        assert require_kiro_v3_admissible(_caps("2.35.1"), GRANT).schema is KAS_0_66_70
        with pytest.raises(KiroV3UnsupportedVersionError):
            require_kiro_v3_admissible(_caps("2.38.0"), GRANT)


@pytest.mark.parametrize("allowed", [None, ["*"]])
def test_the_opt_in_does_not_admit_an_unrestricted_policy(opted_in, caplog, allowed):
    with pytest.raises(KiroV3LaunchError) as excinfo:
        require_kiro_v3_admissible(_caps("2.36.0"), allowed)
    assert excinfo.value.code == "kiro_v3_unrestricted"
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


@pytest.mark.parametrize("allowed", [None, ["*"], ["fs_read", "*"]])
def test_an_unrestricted_policy_refuses(opted_out, allowed):
    with pytest.raises(KiroV3LaunchError) as excinfo:
        require_kiro_v3_admissible(_caps(), allowed)
    assert excinfo.value.code == "kiro_v3_unrestricted"


# --- KIRO_HOME -----------------------------------------------------------------


def test_only_the_default_kiro_home_is_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    require_default_kiro_home([{}, None, {"KIRO_HOME": str(tmp_path / ".kiro")}])
    require_default_kiro_home([{"KIRO_HOME": "~/.kiro"}])
    for env in ({"KIRO_HOME": str(tmp_path / "other")}, {"KIRO_HOME": "/"}):
        with pytest.raises(KiroV3LaunchError) as excinfo:
            require_default_kiro_home([{}, env])
        assert excinfo.value.code == "kiro_v3_home_unsupported"


# --- the installed file and the workspace --------------------------------------


def _launchable(agents, repo, *, env=None, allowed=GRANT, name="reviewer"):
    require_kiro_v3_launchable(name, str(repo), env, list(allowed), KAS_0_66_70, agents)


def test_a_valid_universal_file_and_a_clean_workspace_pass(agents, repo):
    (agents / "reviewer.json").write_text(_universal("reviewer"))
    _launchable(agents, repo)


def test_a_missing_file_refuses_with_the_v3_install_remedy(agents, repo):
    with pytest.raises(KiroV3LaunchError) as excinfo:
        _launchable(agents, repo)
    assert excinfo.value.code == "kiro_v3_not_installed"
    assert "cao install reviewer --provider kiro_cli --engine v3" in str(excinfo.value)


def test_a_v2_only_file_refuses_and_names_any_backup(agents, repo):
    target = agents / "reviewer.json"
    target.write_text(
        json.dumps({"name": "reviewer", "tools": ["read"], "allowedTools": ["fs_read"]})
    )
    (agents / "reviewer.json.bak.1").write_text("{}")
    with pytest.raises(KiroV3InvalidAgentError) as excinfo:
        _launchable(agents, repo)
    message = str(excinfo.value)
    assert "cao install reviewer --provider kiro_cli --engine v3" in message
    assert "reviewer.json.bak.1" in message


def test_a_kiro_rewrite_on_disk_refuses_at_launch(agents, repo):
    data = json.loads(_universal("reviewer"))
    data["kiroUpgrade"] = {"revision": 2}
    (agents / "reviewer.json").write_text(json.dumps(data))
    with pytest.raises(KiroV3InvalidAgentError, match="kiroUpgrade"):
        _launchable(agents, repo)


def test_a_file_whose_grant_exceeds_this_launch_refuses(agents, repo):
    (agents / "reviewer.json").write_text(_universal("reviewer"))
    with pytest.raises(KiroV3InvalidAgentError, match="grant_exceeds_launch"):
        _launchable(agents, repo, allowed=["fs_read", "@cao-mcp-server"])


def test_a_launch_env_kiro_home_refuses(agents, repo, tmp_path):
    (agents / "reviewer.json").write_text(_universal("reviewer"))
    with pytest.raises(KiroV3LaunchError, match="kiro_v3_home_unsupported"):
        _launchable(agents, repo, env={"KIRO_HOME": str(tmp_path / "kh")})


def test_a_workspace_with_kiro_settings_refuses(agents, repo):
    (agents / "reviewer.json").write_text(_universal("reviewer"))
    (repo / ".kiro" / "settings").mkdir(parents=True)
    with pytest.raises(KiroWorkspaceError) as excinfo:
        _launchable(agents, repo)
    assert excinfo.value.code == "kiro_v3_workspace_config:settings"


# --- no absolute server path in any refusal (#1074's contract; #1167 review) ------


def _denied_read(target):
    """``Path.read_bytes`` raising ``PermissionError`` for ``target`` only, as a mode-000
    file does for a non-root server (patched, so the test passes when run as root)."""
    real_read_bytes = Path.read_bytes

    def read_bytes(self):
        if self == target:
            raise PermissionError(13, "Permission denied", str(target))
        return real_read_bytes(self)

    return patch.object(Path, "read_bytes", read_bytes)


@pytest.mark.parametrize("state", ["missing", "directory", "symlink_loop", "permission"])
def test_an_absent_or_unreadable_agent_file_is_coded_and_names_no_path(agents, repo, caplog, state):
    import contextlib
    import os

    target = agents / "reviewer.json"
    if state == "directory":
        target.mkdir()
    elif state == "symlink_loop":
        os.symlink(target, target)
    elif state == "permission":
        target.write_text(_universal("reviewer"))
    denied = _denied_read(target) if state == "permission" else contextlib.nullcontext()
    with (
        denied,
        caplog.at_level("WARNING", logger="cli_agent_orchestrator.services.kiro_v3_launch"),
    ):
        with pytest.raises(KiroV3LaunchError) as excinfo:
            _launchable(agents, repo)
    expected = "kiro_v3_not_installed" if state == "missing" else "kiro_v3_agent_unreadable"
    assert excinfo.value.code == expected
    message = str(excinfo.value)
    if state == "permission":
        assert "PermissionError" in message
    assert str(agents) not in message and os.path.realpath(agents) not in message
    assert "cao install reviewer --provider kiro_cli --engine v3" in message
    # The server log keeps the path for the operator.
    assert any(str(target) in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("name", ["kiro_default", "Default"])
def test_a_fallback_agent_keeps_its_own_refusal_before_any_read(agents, repo, name):
    from cli_agent_orchestrator.providers.kiro_identity import KiroAgentIdentityError

    (agents / f"{name}.json").write_text(_universal(name))
    with (
        patch.object(Path, "read_bytes", side_effect=AssertionError("read")),
        pytest.raises(KiroAgentIdentityError) as excinfo,
    ):
        require_kiro_v3_launchable(name, str(repo), None, GRANT, KAS_0_66_70, agents)
    assert excinfo.value.kind == "fallback_agent"
    assert "--engine v3" not in str(excinfo.value)


def test_backups_and_kiro_home_are_named_without_server_paths(agents, repo, tmp_path, caplog):
    target = agents / "reviewer.json"
    target.write_text(json.dumps({"name": "reviewer", "tools": ["read"]}))
    (agents / "reviewer.json.bak.1").write_text("{}")
    with pytest.raises(KiroV3InvalidAgentError) as excinfo:
        _launchable(agents, repo)
    assert "reviewer.json.bak.1" in str(excinfo.value)
    assert str(agents) not in str(excinfo.value)

    home = str(tmp_path / "elsewhere-home")
    with caplog.at_level("WARNING", logger="cli_agent_orchestrator.services.kiro_v3_launch"):
        with pytest.raises(KiroV3LaunchError) as excinfo:
            require_default_kiro_home([{"KIRO_HOME": home}])
    assert excinfo.value.code == "kiro_v3_home_unsupported"
    assert home not in str(excinfo.value)
    assert any(home in record.getMessage() for record in caplog.records)


def test_a_workspace_refusal_names_no_path_and_logs_it(agents, repo, caplog):
    (agents / "reviewer.json").write_text(_universal("reviewer"))
    (repo / ".kiro" / "hooks").mkdir(parents=True)
    with caplog.at_level("WARNING", logger="cli_agent_orchestrator.providers.kiro_workspace"):
        with pytest.raises(KiroWorkspaceError) as excinfo:
            _launchable(agents, repo)
    assert excinfo.value.code == "kiro_v3_workspace_config:hooks"
    assert str(repo) not in str(excinfo.value)
    assert any(str(repo / ".kiro" / "hooks") in r.getMessage() for r in caplog.records)


def test_no_working_directory_refuses(agents):
    (agents / "reviewer.json").write_text(_universal("reviewer"))
    with pytest.raises(KiroV3LaunchError, match="kiro_v3_bad_cwd"):
        require_kiro_v3_launchable("reviewer", None, None, GRANT, KAS_0_66_70, agents)


# --- through create_terminal: refusals allocate nothing; a pass reaches allocation ---


class Allocated(Exception):
    pass


async def _create(profile, repo, *, version="2.35.1", env_vars=None, allowed=None, seen=None):
    probe = Mock(return_value=_caps(version))
    with (
        patch(f"{_TS}.agent_profiles.load_agent_profile", return_value=profile),
        patch(f"{_TS}.get_backend") as backend,
        patch(f"{_TS}.db_create_terminal") as db_create,
        patch(f"{_TS}.fifo_manager") as fifo,
        patch(f"{_TS}.provider_manager") as providers,
    ):
        backend.return_value.create_session.side_effect = Allocated()
        backend.return_value.session_exists.return_value = False
        try:
            await terminal_service.create_terminal(
                provider="kiro_cli",
                agent_profile=profile.name,
                new_session=True,
                engine="v3",
                working_directory=str(repo),
                env_vars=env_vars,
                allowed_tools=allowed,
                kiro_capability_probe=probe,
            )
        finally:
            if seen is not None:
                seen["session"] = backend.return_value.create_session.called
                seen["other"] = (
                    backend.return_value.create_window.called
                    or db_create.called
                    or fifo.create_reader.called
                    or providers.create_provider.called
                )


PROFILE = AgentProfile(name="reviewer", description="r", allowedTools=GRANT)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setup", "kwargs", "code"),
    [
        ("file", {"version": "2.36.0"}, "kiro_v3_unverified_version"),
        ("file", {"version": "2.37.0"}, "kiro_v3_unverified_version"),
        ("file", {"version": "2.40.0"}, "kiro_v3_unsupported_version"),
        ("file", {"allowed": ["*"]}, "kiro_v3_unrestricted"),
        ("file", {"env_vars": {"KIRO_HOME": "/elsewhere"}}, "kiro_v3_home_unsupported"),
        ("v2file", {}, "kiro_v3_invalid"),
        ("missing", {}, "kiro_v3_not_installed"),
        ("file+hooks", {}, "kiro_v3_workspace_config:hooks"),
    ],
)
async def test_each_pre_allocation_refusal_allocates_nothing(agents, repo, setup, kwargs, code):
    if setup == "missing":
        pass
    elif setup == "v2file":
        (agents / "reviewer.json").write_text(
            json.dumps({"name": "reviewer", "tools": ["fs_read"]})
        )
    else:
        (agents / "reviewer.json").write_text(_universal("reviewer"))
    if setup == "file+hooks":
        (repo / ".kiro" / "hooks").mkdir(parents=True)
    seen: dict = {}
    with pytest.raises(KiroV3Refusal) as excinfo:
        await _create(PROFILE, repo, seen=seen, **kwargs)
    assert excinfo.value.code.startswith(code)
    assert seen == {"session": False, "other": False}


@pytest.mark.asyncio
async def test_a_missing_agent_file_refuses_with_the_v3_install_remedy(agents, repo):
    """The v3 check runs before the v2 resolver, so the remedy names --engine v3
    and a v2-only install is not what the user is told to do (#1167 review)."""
    with pytest.raises(KiroV3LaunchError) as excinfo:
        await _create(PROFILE, repo)
    assert excinfo.value.code == "kiro_v3_not_installed"
    assert "cao install reviewer --provider kiro_cli --engine v3" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_create_that_passes_every_check_reaches_allocation(agents, repo):
    (agents / "reviewer.json").write_text(_universal("reviewer"))
    seen: dict = {}
    with pytest.raises(Allocated):
        await _create(PROFILE, repo, seen=seen)
    assert seen["session"] is True


@pytest.mark.asyncio
async def test_a_bad_kiro_effort_refuses_before_the_probe_with_its_code(agents, repo):
    """A coded refusal, so every create route answers 400 (#1167 review)."""
    profile = AgentProfile(
        name="reviewer", description="r", allowedTools=GRANT, kiroConfig={"effort": "--agent"}
    )
    (agents / "reviewer.json").write_text(_universal("reviewer"))
    probe = Mock()
    with (
        patch(f"{_TS}.agent_profiles.load_agent_profile", return_value=profile),
        patch(f"{_TS}.get_backend") as backend,
        pytest.raises(KiroV3LaunchError) as excinfo,
    ):
        await terminal_service.create_terminal(
            provider="kiro_cli",
            agent_profile="reviewer",
            new_session=True,
            engine="v3",
            working_directory=str(repo),
            kiro_capability_probe=probe,
        )
    assert excinfo.value.code == "kiro_v3_bad_effort"
    assert "kiroConfig.effort" in str(excinfo.value)
    probe.assert_not_called()
    backend.return_value.create_session.assert_not_called()


def test_the_module_reads_the_agents_dir_install_writes(agents):
    from cli_agent_orchestrator.services import install_service

    assert Path(install_service.KIRO_AGENTS_DIR) == agents


@pytest.mark.parametrize(
    ("engine", "verified", "untrusted"),
    [
        ("v3", None, True),
        ("kas", None, True),
        ("v3", False, True),
        ("v3", True, False),
        ("v2", None, False),
        (None, None, False),
    ],
)
def test_a_v3_row_without_a_recorded_identity_is_reclaimed_on_failure(engine, verified, untrusted):
    """v2 rows older than the identity column are grandfathered; no v3 row can be (#1167)."""
    row = {"provider": "kiro_cli", "engine": engine, "identity_verified": verified}
    assert terminal_service._runtime_untrusted_from_row(row) is untrusted


# --- the operator opt-in through create_terminal ---------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setup", "kwargs", "code"),
    [
        ("file", {"allowed": ["*"]}, "kiro_v3_unrestricted"),
        ("file", {"env_vars": {"KIRO_HOME": "/elsewhere"}}, "kiro_v3_home_unsupported"),
        ("v2file", {}, "kiro_v3_invalid"),
        ("missing", {}, "kiro_v3_not_installed"),
        ("file+hooks", {}, "kiro_v3_workspace_config:hooks"),
        ("file", {"version": "2.40.0"}, "kiro_v3_unsupported_version"),
    ],
)
async def test_the_opt_in_widens_no_other_pre_allocation_check(
    opted_in, agents, repo, setup, kwargs, code
):
    """With the opt-in on and 2.36.0 probed, every other refusal still refuses."""

    if setup == "v2file":
        (agents / "reviewer.json").write_text(
            json.dumps({"name": "reviewer", "tools": ["fs_read"]})
        )
    elif setup != "missing":
        (agents / "reviewer.json").write_text(_universal("reviewer"))
    if setup == "file+hooks":
        (repo / ".kiro" / "hooks").mkdir(parents=True)
    seen: dict = {}
    with pytest.raises(KiroV3Refusal) as excinfo:
        await _create(PROFILE, repo, seen=seen, **{"version": "2.36.0", **kwargs})
    assert excinfo.value.code.startswith(code)
    assert seen == {"session": False, "other": False}


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["2.36.0", "2.37.0"])
async def test_the_opt_in_lets_a_pending_create_reach_allocation(opted_in, agents, repo, version):
    (agents / "reviewer.json").write_text(_universal("reviewer"))
    seen: dict = {}
    with pytest.raises(Allocated):
        await _create(PROFILE, repo, version=version, seen=seen)
    assert seen["session"] is True


async def _create_to_completion(repo, *, version):
    """A v3 create that succeeds, with the provider and backend faked."""
    from unittest.mock import AsyncMock, MagicMock

    provider = MagicMock()
    provider.initialize = AsyncMock(return_value=True)
    provider.identity_verified = True
    provider.shell_baseline = None
    with (
        patch(f"{_TS}.agent_profiles.load_agent_profile", return_value=PROFILE),
        patch(f"{_TS}.get_backend") as backend,
        patch(f"{_TS}.db_create_terminal"),
        patch(f"{_TS}.mark_terminal_identity_verified", return_value=True),
        patch(f"{_TS}.delete_terminals_by_session"),
        patch(f"{_TS}.fifo_manager"),
        patch(f"{_TS}.provider_manager") as providers,
        patch(f"{_TS}.generate_terminal_id", return_value="test1234"),
        patch(f"{_TS}.generate_session_name", return_value="session"),
        patch(f"{_TS}.generate_window_name", return_value="reviewer-window"),
        patch(f"{_TS}.get_herdr_inbox_service", return_value=None),
    ):
        backend.return_value.session_exists.return_value = False
        backend.return_value.supports_event_inbox.return_value = True
        providers.create_provider.return_value = provider
        return await terminal_service.create_terminal(
            provider="kiro_cli",
            agent_profile=PROFILE.name,
            new_session=True,
            engine="v3",
            working_directory=str(repo),
            kiro_capability_probe=Mock(return_value=_caps(version)),
        )


@pytest.mark.asyncio
async def test_the_launch_response_names_an_opted_in_pending_version(opted_in, agents, repo):
    (agents / "reviewer.json").write_text(_universal("reviewer"))
    terminal = await _create_to_completion(repo, version="2.36.0")
    (note,) = terminal.launch_notes
    assert "kiro-cli 2.36.0 is pending operator verification" in note
    assert terminal.model_dump(mode="json")["launch_notes"] == [note]


@pytest.mark.asyncio
async def test_a_verified_launch_response_carries_no_note(opted_in, agents, repo):
    (agents / "reviewer.json").write_text(_universal("reviewer"))
    terminal = await _create_to_completion(repo, version="2.35.1")
    assert terminal.launch_notes is None
