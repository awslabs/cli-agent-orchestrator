import asyncio
import fcntl
import threading

import pytest

from cli_agent_orchestrator.decisions import settings
from cli_agent_orchestrator.services import settings_service


@pytest.fixture
def egress(monkeypatch, tmp_path):
    from cli_agent_orchestrator.decisions import egress

    monkeypatch.setattr(egress, "HALT_FILE", tmp_path / "decisions.halt")
    monkeypatch.setattr(egress, "EGRESS_LOCK_FILE", tmp_path / "decisions-egress.lock")
    monkeypatch.setattr(egress, "_settings_loader", settings.load_settings)
    monkeypatch.setattr(settings_service, "CAO_HOME_DIR", tmp_path)
    for key in settings.STATE_ENV.values():
        monkeypatch.delenv(key, raising=False)
    return egress


def test_halt_beats_flags_and_environment(egress, monkeypatch):
    settings.set_point("model.route", "shadow", "external")
    egress.HALT_FILE.touch()
    monkeypatch.setenv("CAO_DECISION_MODEL_ROUTE", "on")
    # `cao-server --decision` writes the same variables into the server's startup snapshot.
    for snapshot in (
        settings.load_settings(),
        settings.load_settings(
            environment={"CAO_DECISION_MODEL_ROUTE": "on", "CAO_DECISION_EFFORT_ROUTE": "on"}
        ),
    ):
        assert all(point.state.value == "off" for point in snapshot.points.values())
        assert snapshot.points["model.route"].decider == "external"


@pytest.mark.parametrize("state", ["shadow", "on", "off"])
def test_set_point_clears_halt_only_when_enabling(egress, state):
    egress.HALT_FILE.touch()
    settings.set_point("model.route", state)
    assert egress.is_halted() == (state == "off")
    assert settings.load_settings().points["model.route"].state.value == state


def test_failed_set_does_not_clear_halt(egress):
    egress.HALT_FILE.touch()
    settings_service.SETTINGS_FILE.write_text("invalid")
    with pytest.raises(settings_service.SettingsUnreadableError):
        settings.set_point("model.route", "on")
    assert egress.is_halted()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,binding,point,halted,expected",
    [
        ("shadow", "external", "model.route", False, True),
        ("on", "external", "model.route", False, True),
        ("off", "external", "model.route", False, False),
        ("on", "other", "model.route", False, False),
        ("on", "external", "unknown", False, False),
        ("on", "external", "model.route", True, False),
    ],
)
async def test_permit_checks_fresh_binding_and_state(
    egress, state, binding, point, halted, expected
):
    settings.set_point("model.route", state, binding)
    if halted:
        egress.HALT_FILE.touch()
    async with egress.egress_permit(point, "external") as allowed:
        assert allowed is expected
        if allowed:
            assert not egress.drain(0)
    assert egress.drain(0)


@pytest.mark.asyncio
async def test_permit_refused_for_unreadable_settings_and_lock(egress, monkeypatch, tmp_path):
    settings_service.SETTINGS_FILE.write_text("invalid")
    async with egress.egress_permit("model.route", "external") as allowed:
        assert not allowed
    monkeypatch.setattr(egress, "EGRESS_LOCK_FILE", tmp_path)
    async with egress.egress_permit("model.route", "external") as allowed:
        assert not allowed
    assert not egress.drain(0.01)


@pytest.mark.asyncio
async def test_permit_refused_during_exclusive_drain(egress):
    settings.set_point("model.route", "on", "external")
    with egress.EGRESS_LOCK_FILE.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        async with egress.egress_permit("model.route", "external") as allowed:
            assert not allowed
    assert egress.drain(0)


@pytest.mark.asyncio
async def test_permit_uses_startup_snapshot_and_fresh_file(egress, monkeypatch):
    settings.set_point("model.route", "off", "external")
    startup = {"CAO_DECISION_MODEL_ROUTE": "shadow"}
    loader = lambda: settings.load_settings(environment=startup)
    egress.set_settings_loader(loader)
    monkeypatch.setenv("CAO_DECISION_MODEL_ROUTE", "off")
    async with egress.egress_permit("model.route", "external") as allowed:
        assert allowed
        assert loader().points["model.route"].state.value == "shadow"
    settings.set_point("model.route", "off", "other")
    async with egress.egress_permit("model.route", "external") as allowed:
        assert not allowed
    egress.HALT_FILE.touch()
    async with egress.egress_permit("model.route", "other") as allowed:
        assert not allowed


@pytest.mark.asyncio
async def test_permit_checks_the_marker_before_and_after_reading_settings(egress):
    # The loader ignores the marker, so only the permit's own checks can refuse.
    settings.set_point("model.route", "on", "external")
    snapshot = settings.load_settings()
    reads = []

    def loader():
        reads.append(egress.is_halted())
        return snapshot

    egress.set_settings_loader(loader)
    async with egress.egress_permit("model.route", "external") as allowed:
        assert allowed
    egress.HALT_FILE.touch()
    async with egress.egress_permit("model.route", "external") as allowed:
        assert not allowed
    assert reads == [False]  # a halted permit reads no settings

    def halt_while_reading():
        egress.HALT_FILE.touch()
        return snapshot

    egress.HALT_FILE.unlink()
    egress.set_settings_loader(halt_while_reading)
    async with egress.egress_permit("model.route", "external") as allowed:
        assert not allowed


@pytest.mark.asyncio
async def test_cancellation_releases_permit(egress):
    settings.set_point("model.route", "on", "external")
    entered = asyncio.Event()

    async def hold():
        async with egress.egress_permit("model.route", "external") as allowed:
            assert allowed
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(hold())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert egress.drain(0)


def test_drain_waits_for_real_shared_lock(egress):
    acquired = threading.Event()
    release = threading.Event()

    def hold():
        with egress.EGRESS_LOCK_FILE.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            acquired.set()
            assert release.wait(2)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert acquired.wait(2)
        assert not egress.drain(0.02)
        timer = threading.Timer(0.05, release.set)
        timer.start()
        assert egress.drain(1)
        timer.join()
    finally:
        release.set()
        thread.join(2)


def test_new_home_files_are_redirected(monkeypatch, tmp_path):
    from cli_agent_orchestrator.decisions import egress

    assert egress.HALT_FILE == tmp_path / "decisions.halt"
    assert egress.EGRESS_LOCK_FILE == tmp_path / "decisions-egress.lock"
    from cli_agent_orchestrator.constants import CAO_HOME_DIR

    originals = {CAO_HOME_DIR / "decisions.halt", CAO_HOME_DIR / "decisions-egress.lock"}
    assert egress.HALT_FILE not in originals
    assert egress.EGRESS_LOCK_FILE not in originals
    # Fail before an open can create either unredirected file; no real-home I/O.
    import os

    original_open = os.open

    def guarded_open(path, *args, **kwargs):
        from pathlib import Path

        assert Path(path) not in originals
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded_open)
    egress.HALT_FILE.touch()
    assert egress.drain(0)


@pytest.mark.asyncio
async def test_server_and_permit_share_startup_loader(egress, monkeypatch):
    from test.api.test_lifespan_inbox import _patched_lifespan
    from unittest.mock import AsyncMock, MagicMock

    from cli_agent_orchestrator.api import main
    from cli_agent_orchestrator.decisions.store import DecisionStore

    settings.set_point("model.route", "off", "external")
    monkeypatch.setenv("CAO_DECISION_MODEL_ROUTE", "shadow")
    monkeypatch.setattr(DecisionStore, "sweep", lambda self: None)
    monkeypatch.setattr(
        main.terminal_service,
        "recover_interrupted_deferred_init_external_owners",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(main, "_seed_default_skills_at_startup", lambda: None)
    monkeypatch.setattr(main, "_reconcile_memory_at_startup", lambda: None)
    with _patched_lifespan(MagicMock(), []):
        async with main.lifespan(main.app):
            monkeypatch.setenv("CAO_DECISION_MODEL_ROUTE", "off")
            assert (
                main.app.state.decision_engine.settings_loader().points["model.route"].state.value
                == "shadow"
            )
            async with egress.egress_permit("model.route", "external") as allowed:
                assert allowed
            egress.HALT_FILE.touch()
            assert (
                main.app.state.decision_engine.settings_loader().points["model.route"].state.value
                == "off"
            )
            async with egress.egress_permit("model.route", "external") as allowed:
                assert not allowed


@pytest.mark.parametrize("timeout", [-1, float("nan"), float("inf")])
def test_invalid_drain_timeout_refused(egress, timeout):
    assert not egress.drain(timeout)


def test_unreadable_or_dangling_halt_fails_closed(egress, monkeypatch, tmp_path):
    from pathlib import Path

    egress.HALT_FILE.symlink_to(tmp_path / "missing")
    assert egress.is_halted()
    original = Path.lstat

    def lstat(path, *args, **kwargs):
        if path == egress.HALT_FILE:
            raise PermissionError("marker unavailable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", lstat)
    assert egress.is_halted()
    assert {p.state.value for p in settings.load_settings().points.values()} == {"off"}


@pytest.mark.asyncio
async def test_halt_published_after_shared_lock_refuses_permit(egress, monkeypatch):
    settings.set_point("model.route", "on", "external")
    actual_flock = egress.fcntl.flock

    def flock(fd, operation):
        actual_flock(fd, operation)
        if operation == fcntl.LOCK_SH | fcntl.LOCK_NB:
            egress.HALT_FILE.touch()

    monkeypatch.setattr(egress.fcntl, "flock", flock)
    async with egress.egress_permit("model.route", "external") as allowed:
        assert allowed is False
    assert egress.is_halted()
