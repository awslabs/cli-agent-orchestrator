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


def test_a_measured_version_and_a_restricted_policy_are_admitted():
    assert require_kiro_v3_admissible(_caps(), GRANT) is KAS_0_66_70


@pytest.mark.parametrize(
    ("caps", "error"),
    [
        (_caps("2.36.0"), KiroV3UnverifiedVersionError),
        (_caps("2.37.0"), KiroV3UnsupportedVersionError),
        (_caps(None), KiroV3UnsupportedVersionError),
        (Mock(), KiroV3UnsupportedVersionError),
    ],
)
def test_versions_outside_the_admitted_list_refuse(caps, error):
    with pytest.raises(error):
        require_kiro_v3_admissible(caps, GRANT)


def test_the_pending_switch_admits_a_pending_version(monkeypatch):
    from cli_agent_orchestrator.providers import kiro_v3_schema

    monkeypatch.setattr(kiro_v3_schema, "KIRO_V3_ADMIT_PENDING_VERSIONS", True)
    assert require_kiro_v3_admissible(_caps("2.36.0"), GRANT) is KAS_0_66_70


@pytest.mark.parametrize("allowed", [None, ["*"], ["fs_read", "*"]])
def test_an_unrestricted_policy_refuses(allowed):
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
        ("file", {"version": "2.40.0"}, "kiro_v3_unsupported_version"),
        ("file", {"allowed": ["*"]}, "kiro_v3_unrestricted"),
        ("file", {"env_vars": {"KIRO_HOME": "/elsewhere"}}, "kiro_v3_home_unsupported"),
        ("v2file", {}, "kiro_v3_invalid"),
        ("file+hooks", {}, "kiro_v3_workspace_config:hooks"),
    ],
)
async def test_each_pre_allocation_refusal_allocates_nothing(agents, repo, setup, kwargs, code):
    if setup == "v2file":
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
async def test_a_create_that_passes_every_check_reaches_allocation(agents, repo):
    (agents / "reviewer.json").write_text(_universal("reviewer"))
    seen: dict = {}
    with pytest.raises(Allocated):
        await _create(PROFILE, repo, seen=seen)
    assert seen["session"] is True


@pytest.mark.asyncio
async def test_a_bad_kiro_effort_refuses_before_the_probe(agents, repo):
    profile = AgentProfile(
        name="reviewer", description="r", allowedTools=GRANT, kiroConfig={"effort": "--agent"}
    )
    (agents / "reviewer.json").write_text(_universal("reviewer"))
    with pytest.raises(ValueError, match="kiroConfig.effort"):
        await _create(profile, repo)


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
