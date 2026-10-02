"""The ephemeral namespace is launch-only and fails closed."""

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.utils import agent_profiles as profiles

NAME = "Ramones-log_triage-3f9a"
DOCUMENT = f"---\nname: {NAME}\ndescription: test\nprovider: claude_code\n---\nTask.\n"


@pytest.fixture
def stores(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services import settings_service

    local = tmp_path / "installed"
    local.mkdir()
    live = tmp_path / "ephemeral" / "live"
    live.mkdir(parents=True)
    monkeypatch.setattr(profiles, "LOCAL_AGENT_STORE_DIR", local)
    monkeypatch.setattr(profiles, "EPHEMERAL_LIVE_DIR", live, raising=False)
    monkeypatch.setattr(settings_service, "get_agent_dirs", lambda: {})
    monkeypatch.setattr(settings_service, "get_extra_agent_dirs", lambda: [])
    monkeypatch.setattr(settings_service, "get_disabled_agent_dirs", lambda: [])
    return local, live


@pytest.mark.parametrize(
    "name, expected",
    [
        (NAME, True),
        ("developer", False),
        ("R-x-1234", False),
        ("Ra-abc-ABCD", False),
        (NAME + "\n", False),
    ],
)
def test_reserved_pattern(name, expected):
    assert profiles.routes_to_ephemeral_store(name) is expected


def test_missing_never_falls_back(stores):
    local, _ = stores
    (local / f"{NAME}.md").write_text(DOCUMENT)
    for lookup in (
        profiles.load_launch_profile,
        profiles.resolve_agent_profile_source,
        lambda n: profiles.resolve_provider(n, "copilot_cli"),
    ):
        with pytest.raises(profiles.EphemeralProfileUnavailable):
            lookup(NAME)
    with pytest.raises(FileNotFoundError):
        profiles.load_agent_profile(NAME)


def test_served_store_determines_source(stores):
    local, live = stores
    (live / f"{NAME}.md").write_text(DOCUMENT)
    profile, source = profiles.load_launch_profile(NAME)
    assert profile.name == NAME
    assert source == profiles.ProfileSource.EPHEMERAL
    assert profiles.resolve_agent_profile_source(NAME) == source
    (local / "ordinary.md").write_text("---\nname: ordinary\ndescription: test\n---\nTask")
    assert profiles.load_launch_profile("ordinary")[1] == profiles.ProfileSource.INSTALLED
    assert profiles.resolve_agent_profile_source("ordinary") == profiles.ProfileSource.INSTALLED
    assert NAME not in {p["name"] for p in profiles.list_agent_profiles()}


def test_all_consumers_use_module_predicate(stores, monkeypatch, caplog):
    local, live = stores
    name = "ordinary"
    (local / f"{name}.md").write_text(DOCUMENT)
    (live / f"{name}.md").write_text(DOCUMENT)
    predicate = Mock(return_value=True)
    monkeypatch.setattr(profiles, "routes_to_ephemeral_store", predicate)
    assert profiles.load_launch_profile(name)[1] == profiles.ProfileSource.EPHEMERAL
    assert profiles.resolve_agent_profile_source(name) == profiles.ProfileSource.EPHEMERAL
    with pytest.raises(FileNotFoundError):
        profiles._read_agent_profile_source(name)
    profiles.warn_reserved_installed_profiles()
    assert name in caplog.text
    assert predicate.call_count >= 4


def test_installed_collision_warns_at_startup(stores, caplog):
    local, _ = stores
    (local / f"{NAME}.md").write_text(DOCUMENT)
    profiles.warn_reserved_installed_profiles()
    assert NAME in caplog.text
    assert "reserved" in caplog.text.lower()


@pytest.mark.parametrize("path", [f"/agents/profiles/{NAME}", f"/agents/profiles/{NAME}/source"])
def test_profile_reads_refuse(stores, path):
    from cli_agent_orchestrator.api.main import app

    local, live = stores
    (local / f"{NAME}.md").write_text(DOCUMENT)
    (live / f"{NAME}.md").write_text(DOCUMENT)
    assert TestClient(app, base_url="http://localhost").get(path).status_code == 404


@pytest.mark.parametrize("method", ["post", "put"])
def test_profile_writes_refuse(stores, method):
    from cli_agent_orchestrator.api.main import app

    client = TestClient(app, base_url="http://localhost")
    body = {"content": DOCUMENT}
    path = f"/agents/profiles/{NAME}"
    if method == "post":
        body["name"] = NAME
        path = "/agents/profiles"
    assert getattr(client, method)(path, json=body).status_code == 400
    assert not (stores[0] / f"{NAME}.md").exists()


def test_cli_profile_refuses(stores):
    from cli_agent_orchestrator.cli.commands.profile import (
        _read_profile_text,
        _resolve_profile_path,
    )

    (stores[0] / f"{NAME}.md").write_text(DOCUMENT)
    assert _read_profile_text(NAME) is None
    assert _resolve_profile_path(NAME) is None
    from click.testing import CliRunner

    from cli_agent_orchestrator.cli.commands.profile import profile

    result = CliRunner().invoke(profile, ["show", NAME])
    assert result.exit_code != 0
    assert "not found" in result.output
    assert "Task." not in result.output


def test_install_refuses(stores):
    from cli_agent_orchestrator.services.install_service import install_agent

    (stores[0] / f"{NAME}.md").write_text(DOCUMENT)
    assert not install_agent(NAME, provider="claude_code").success


@pytest.mark.asyncio
async def test_startup_calls_warning(stores, monkeypatch, caplog):
    from unittest.mock import AsyncMock

    from cli_agent_orchestrator.api import main

    (stores[0] / f"{NAME}.md").write_text(DOCUMENT)
    monkeypatch.setattr(main, "setup_logging", lambda: None)
    monkeypatch.setattr(main, "init_telemetry", lambda _: None)
    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr(
        main.terminal_service,
        "recover_interrupted_deferred_init_external_owners",
        AsyncMock(side_effect=RuntimeError("stop before runtime startup")),
    )
    with pytest.raises(RuntimeError, match="stop before runtime startup"):
        async with main.lifespan(main.app):
            pytest.fail("runtime must not start")
    assert NAME in caplog.text


def test_find_profiles_never_lists_live_or_shadowed_names(stores):
    from cli_agent_orchestrator.mcp_server import server

    for store in stores:
        (store / f"{NAME}.md").write_text(DOCUMENT)
    assert NAME not in {p["name"] for p in server.find_profiles("log triage", limit=100)}


@pytest.mark.parametrize("bad_file", ["symlink", "invalid", "directory"])
def test_unusable_live_file_fails_closed(stores, tmp_path, bad_file):
    path = stores[1] / f"{NAME}.md"
    if bad_file == "symlink":
        outside = tmp_path / "outside.md"
        outside.write_text(DOCUMENT)
        path.symlink_to(outside)
    elif bad_file == "invalid":
        path.write_text("---\nallowedTools: 9\n---\nTask")
    else:
        path.mkdir()
    with pytest.raises(profiles.EphemeralProfileUnavailable):
        profiles.load_launch_profile(NAME)
