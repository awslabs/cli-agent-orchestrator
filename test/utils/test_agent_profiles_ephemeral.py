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


def test_served_store_determines_source(stores, monkeypatch):
    monkeypatch.setattr(profiles, "resolve_env_vars", lambda content: content)
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


@pytest.mark.parametrize(
    "consumer", ["launch", "source", "raw", "warning", "list", "post", "put", "create", "handoff"]
)
def test_all_consumers_use_module_predicate(stores, monkeypatch, caplog, consumer):
    monkeypatch.setattr(profiles, "resolve_env_vars", lambda content: content)
    from cli_agent_orchestrator.api.main import app
    from cli_agent_orchestrator.services import profile_store
    from cli_agent_orchestrator.utils import orchestration

    local, live = stores
    name = "ordinary"
    document = DOCUMENT.replace(NAME, name)
    (local / f"{name}.md").write_text(document)
    (live / f"{name}.md").write_text(document)
    writer = Mock()
    monkeypatch.setattr(profile_store, "write_profile", writer)
    monkeypatch.setattr(profile_store, "replace_profile", writer)
    monkeypatch.setattr(orchestration, "_current_terminal_id", lambda: None)
    monkeypatch.setattr(orchestration, "resolve_provider", lambda *a, **k: "claude_code")
    monkeypatch.setattr(
        orchestration.requests,
        "post",
        Mock(
            return_value=Mock(
                json=lambda: {
                    "id": "abcd1234",
                    "session_name": "session",
                    "provider": "claude_code",
                }
            )
        ),
    )

    def invoke():
        if consumer == "launch":
            return profiles.load_launch_profile(name)[1]
        if consumer == "source":
            return profiles.resolve_agent_profile_source(name)
        if consumer == "raw":
            return profiles._read_agent_profile_source(name)
        if consumer == "warning":
            profiles.warn_reserved_installed_profiles()
            return any("ordinary" in record.message for record in caplog.records)
        if consumer == "list":
            return name in {profile["name"] for profile in profiles.list_agent_profiles()}
        if consumer in {"post", "put"}:
            client = TestClient(app, base_url="http://localhost")
            body = {"content": document}
            path = f"/agents/profiles/{name}"
            if consumer == "post":
                body["name"] = name
                path = "/agents/profiles"
            return getattr(client, consumer)(path, json=body).status_code
        if consumer == "create":
            return orchestration._create_terminal(name)
        return orchestration._resolve_handoff_provider(name)

    initial = invoke()
    if consumer in {"launch", "source"}:
        assert initial == profiles.ProfileSource.INSTALLED
    elif consumer == "raw":
        assert initial == document
    elif consumer == "warning":
        assert initial is False
    elif consumer == "list":
        assert initial is True
    elif consumer in {"post", "put"}:
        assert initial == (201 if consumer == "post" else 200)
        writer.assert_called_once()
        writer.reset_mock()
    elif consumer == "create":
        assert initial == ("abcd1234", "claude_code")
    else:
        assert initial.provider == "claude_code"

    caplog.clear()
    monkeypatch.setattr(profiles, "routes_to_ephemeral_store", lambda _: True)
    if consumer in {"raw", "create", "handoff"}:
        with pytest.raises(FileNotFoundError if consumer == "raw" else ValueError):
            invoke()
    else:
        patched = invoke()
        if consumer in {"launch", "source"}:
            assert patched == profiles.ProfileSource.EPHEMERAL
        elif consumer == "warning":
            assert patched is True
        elif consumer == "list":
            assert patched is False
        else:
            assert patched == 400
            writer.assert_not_called()


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


@pytest.mark.parametrize("scan_error", [PermissionError, NotADirectoryError])
@pytest.mark.asyncio
async def test_failed_reserved_scan_does_not_abort_startup(stores, monkeypatch, caplog, scan_error):
    from unittest.mock import AsyncMock

    from cli_agent_orchestrator.api import main

    monkeypatch.setattr(main, "setup_logging", lambda: None)
    monkeypatch.setattr(main, "init_telemetry", lambda _: None)
    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr(
        profiles, "_list_installed_profiles", Mock(side_effect=scan_error("unreadable store"))
    )
    recovery = AsyncMock(side_effect=RuntimeError("post-warning startup reached"))
    monkeypatch.setattr(
        main.terminal_service, "recover_interrupted_deferred_init_external_owners", recovery
    )
    with pytest.raises(RuntimeError, match="post-warning startup reached"):
        async with main.lifespan(main.app):
            pytest.fail("stop before opening runtime resources")
    recovery.assert_awaited_once()
    warnings = [
        record
        for record in caplog.records
        if record.name == profiles.__name__ and record.levelname == "WARNING"
    ]
    assert len(warnings) == 1
    assert warnings[0].message == "reserved-name startup scan failed"
    assert warnings[0].exc_info[0] is scan_error


@pytest.mark.parametrize("reserved", [True, False])
def test_launch_environment_resolution_is_installed_only(stores, monkeypatch, reserved):
    from string import Template

    body = "${X} | $X | $$"
    name = NAME if reserved else "ordinary"
    document = DOCUMENT.replace(NAME, name).replace("Task.", body)
    (stores[1 if reserved else 0] / f"{name}.md").write_text(document)
    fake = Mock(
        side_effect=lambda value: Template(value).safe_substitute({"X": "dummy-mapped-value"})
    )
    monkeypatch.setattr(profiles, "resolve_env_vars", fake)
    loaded, source = profiles.load_launch_profile(name)
    if reserved:
        assert loaded.system_prompt.encode("utf-8") == body.encode("utf-8")
        fake.assert_not_called()
        assert source == profiles.ProfileSource.EPHEMERAL
    else:
        assert loaded.system_prompt == "dummy-mapped-value | dummy-mapped-value | $"
        fake.assert_called_once_with(document)
        assert source == profiles.ProfileSource.INSTALLED


@pytest.mark.parametrize("shape", ["fifo", "directory", "symlink"])
def test_nonregular_live_profile_refuses_without_blocking(stores, shape):
    import os
    import threading

    _, live = stores
    path = live / (NAME + ".md")
    if shape == "fifo":
        os.mkfifo(path)
    elif shape == "directory":
        path.mkdir()
    else:
        target = live / "target.md"
        target.write_text(DOCUMENT)
        path.symlink_to(target)
    done = threading.Event()
    errors = []

    def load():
        try:
            profiles.load_launch_profile(NAME)
        except profiles.EphemeralProfileUnavailable as exc:
            errors.append(exc)
        finally:
            done.set()

    worker = threading.Thread(target=load, daemon=True)
    worker.start()
    completed = done.wait(timeout=2)
    if not completed:
        fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
    worker.join(timeout=2)
    assert completed, "live profile read blocked on FIFO"
    assert len(errors) == 1
