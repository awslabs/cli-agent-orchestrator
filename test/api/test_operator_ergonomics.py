"""Operator-facing ergonomics on the HTTP API: working-directory
normalization at the session/terminal creation boundary, the server-side
folder listing, per-session display labels, and the ``X-Server-Time`` header.
"""

import asyncio
import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from cli_agent_orchestrator.models.terminal import Terminal
from cli_agent_orchestrator.services import settings_service
from cli_agent_orchestrator.utils.paths import WorkingDirectoryError


@pytest.fixture
def settings_file(tmp_path):
    """Isolate settings.json so label writes never touch the real config."""
    fake = tmp_path / "settings.json"
    with (
        patch("cli_agent_orchestrator.services.settings_service.SETTINGS_FILE", fake),
        patch("cli_agent_orchestrator.services.settings_service.CAO_HOME_DIR", tmp_path),
    ):
        yield fake


@pytest.fixture
def backend():
    """The session backend as the label endpoint sees it; sessions exist by default."""
    with patch("cli_agent_orchestrator.api.main.get_backend") as get_backend:
        get_backend.return_value.session_exists.return_value = True
        yield get_backend.return_value


class TestServerTimeHeader:
    def test_every_response_carries_an_offset_aware_iso_timestamp(self, client):
        resp = client.get("/health")
        stamp = resp.headers.get("X-Server-Time")
        assert stamp, "X-Server-Time missing"
        parsed = datetime.fromisoformat(stamp)
        # Offset-aware: a naive string would be read as browser-local time and
        # reintroduce the skew the header exists to remove.
        assert parsed.tzinfo is not None
        assert abs((datetime.now().astimezone() - parsed).total_seconds()) < 60

    def test_error_responses_carry_it_too(self, client):
        resp = client.get("/sessions/definitely-not-a-session")
        assert resp.status_code >= 400
        assert "X-Server-Time" in resp.headers

    def test_cross_origin_browsers_are_allowed_to_read_it(self, client):
        """Not a CORS-safelisted header: unless the server exposes it, a page
        on another allowed origin gets the response but ``fetch`` hides the
        header, and the skew correction silently never happens."""
        resp = client.get("/health", headers={"Origin": "http://localhost:3000"})
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == "http://localhost:3000"
        exposed = resp.headers.get("access-control-expose-headers", "").lower()
        assert "x-server-time" in exposed


class TestFsDirs:
    def test_lists_only_directories_visible_first(self, client, tmp_path):
        (tmp_path / "beta").mkdir()
        (tmp_path / "alpha").mkdir()
        (tmp_path / ".hidden").mkdir()
        (tmp_path / "a_file.txt").write_text("x")

        resp = client.get("/fs/dirs", params={"path": str(tmp_path)})
        assert resp.status_code == 200
        body = resp.json()
        assert body["dirs"] == ["alpha", "beta", ".hidden"]
        assert body["path"] == str(tmp_path)
        assert body["parent"] == str(tmp_path.parent)

    def test_missing_folder_is_a_clear_400_and_is_not_created(self, client, tmp_path):
        target = tmp_path / "definitely" / "not" / "here"
        resp = client.get("/fs/dirs", params={"path": str(target)})
        assert resp.status_code == 400
        assert "does not exist" in resp.json()["detail"]
        assert not target.exists(), "a listing must never create the folder it was asked about"

    def test_file_path_is_a_clear_400(self, client, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("x")
        resp = client.get("/fs/dirs", params={"path": str(f)})
        assert resp.status_code == 400
        assert "is not a folder" in resp.json()["detail"]

    def test_defaults_to_home(self, client):
        resp = client.get("/fs/dirs")
        assert resp.status_code == 200
        assert resp.json()["path"] == str(Path.home().resolve())

    def test_blank_or_quoted_empty_path_falls_back_to_home_not_cwd(self, client):
        """A quotes-only value normalizes to None; ``Path("")`` would silently
        resolve to the server's CWD instead of the documented default."""
        for blank in ('""', "   ", "''"):
            resp = client.get("/fs/dirs", params={"path": blank})
            assert resp.status_code == 200, blank
            assert resp.json()["path"] == str(Path.home().resolve()), blank

    def test_unreadable_folder_other_oserror_is_a_400_not_a_500(self, client, tmp_path):
        """ENOTDIR/EIO/a stale mount: the per-entry loop already tolerates
        these, so the directory-level read must not 500 either."""
        with patch(
            "cli_agent_orchestrator.api.main.Path.iterdir", side_effect=OSError("stale mount")
        ):
            resp = client.get("/fs/dirs", params={"path": str(tmp_path)})
        assert resp.status_code == 400
        assert "stale mount" in resp.json()["detail"]

    def test_unknown_user_tilde_is_a_400_not_a_500(self, client):
        resp = client.get("/fs/dirs", params={"path": "~no-such-user-here/proj"})
        assert resp.status_code == 400
        assert "Cannot expand" in resp.json()["detail"]

    def test_root_has_no_parent(self, client):
        resp = client.get("/fs/dirs", params={"path": "/"})
        assert resp.status_code == 200
        assert resp.json()["parent"] is None

    def test_unreadable_folder_is_a_400_not_a_500(self, client, tmp_path):
        # Simulate EACCES on iterdir without depending on running as non-root.
        with patch("cli_agent_orchestrator.api.main.Path.iterdir", side_effect=PermissionError):
            resp = client.get("/fs/dirs", params={"path": str(tmp_path)})
        assert resp.status_code == 400
        assert "permission" in resp.json()["detail"].lower()


class TestSessionLabelEndpoint:
    def test_label_roundtrip_and_clear(self, client, settings_file, backend):
        resp = client.post("/sessions/cao-demo/label", json={"label": "  My Run  "})
        assert resp.status_code == 200
        assert resp.json() == {"session_name": "cao-demo", "label": "My Run"}
        assert settings_service.get_session_labels() == {"cao-demo": "My Run"}

        resp2 = client.post("/sessions/cao-demo/label", json={"label": ""})
        assert resp2.status_code == 200
        assert resp2.json()["label"] is None
        assert settings_service.get_session_labels() == {}

    def test_malformed_session_name_is_a_400(self, client, settings_file):
        resp = client.post("/sessions/bad:name/label", json={"label": "x"})
        assert resp.status_code == 400
        assert settings_service.get_session_labels() == {}

    def test_unprefixed_name_addresses_the_same_session(self, client, settings_file, backend):
        """``POST /sessions`` turns ``demo`` into ``cao-demo`` at creation, and
        every read and the teardown key off that prefixed id. Labelling with
        the name the operator created with must reach the same session, not
        store an entry that never surfaces and is never cleared."""
        resp = client.post("/sessions/demo/label", json={"label": "My Run"})
        assert resp.status_code == 200
        assert resp.json() == {"session_name": "cao-demo", "label": "My Run"}
        assert settings_service.get_session_labels() == {"cao-demo": "My Run"}

        # ...and the prefixed spelling is the same entry, not a second one.
        client.post("/sessions/cao-demo/label", json={"label": "Renamed"})
        assert settings_service.get_session_labels() == {"cao-demo": "Renamed"}

    def test_label_is_required(self, client, settings_file):
        resp = client.post("/sessions/cao-demo/label", json={})
        assert resp.status_code == 422

    def test_label_over_the_cap_is_a_422_not_a_silent_truncation(
        self, client, settings_file, backend
    ):
        """The Pydantic bound used to be 1024 while the service capped at 60,
        so 61-1024 characters were silently truncated. One cap now."""
        too_long = "x" * (settings_service.SESSION_LABEL_MAX_LENGTH + 1)
        resp = client.post("/sessions/cao-demo/label", json={"label": too_long})
        assert resp.status_code == 422
        assert settings_service.get_session_labels() == {}
        at_cap = "y" * settings_service.SESSION_LABEL_MAX_LENGTH
        assert client.post("/sessions/cao-demo/label", json={"label": at_cap}).status_code == 200

    def test_control_characters_are_a_400_and_store_nothing(self, client, settings_file, backend):
        """``"ok\\x1b[2J\\nrm -rf /"`` was stored and echoed verbatim: an
        ANSI-escape injection into any CLI listing that prints labels."""
        resp = client.post("/sessions/cao-demo/label", json={"label": "ok\x1b[2J\nrm -rf /"})
        assert resp.status_code == 400
        assert "printable" in resp.json()["detail"]
        assert settings_service.get_session_labels() == {}

    def test_get_accepts_the_bare_name_the_label_endpoint_accepts(self, client, settings_file):
        """``POST /sessions/demo/label`` succeeded (normalized to ``cao-demo``)
        and ``GET /sessions/demo`` 404'd right after. One helper now, used by
        create, GET, DELETE and label."""
        with patch("cli_agent_orchestrator.api.main.session_service") as svc:
            svc.get_session.return_value = {"session": {"id": "cao-demo"}, "terminals": []}
            assert client.get("/sessions/demo").status_code == 200
            svc.get_session.assert_called_once_with("cao-demo")
            svc.delete_session.return_value = {"deleted": ["cao-demo"], "errors": []}
            assert client.delete("/sessions/demo").status_code == 200
            assert svc.delete_session.call_args.args[0] == "cao-demo"

    def test_unknown_session_is_a_404_and_stores_nothing(self, client, settings_file, backend):
        """An orphan label is never read and never cleared; refuse it up front."""
        backend.session_exists.return_value = False
        resp = client.post("/sessions/cao-ghost/label", json={"label": "Boo"})
        assert resp.status_code == 404
        backend.session_exists.assert_called_once_with("cao-ghost")
        assert settings_service.get_session_labels() == {}
        assert not settings_file.exists()

    def test_clearing_is_allowed_once_the_session_is_gone(self, client, settings_file, backend):
        """A session killed outside ``delete_session`` leaves its label behind,
        and this endpoint is the only way to remove it."""
        client.post("/sessions/cao-demo/label", json={"label": "Run"})
        backend.session_exists.return_value = False
        resp = client.post("/sessions/cao-demo/label", json={"label": ""})
        assert resp.status_code == 200
        assert resp.json()["label"] is None
        assert settings_service.get_session_labels() == {}

    def test_unreadable_settings_file_is_a_500_that_leaves_it_alone(
        self, client, settings_file, backend
    ):
        """#737: the write used to replace an unparseable settings.json with a
        label-only one. Now it is refused; the body names the problem without
        the server path."""
        settings_file.write_text('{"agent_dirs": {"kiro_cli": "/keep"},}')  # trailing comma
        before = settings_file.read_bytes()
        resp = client.post("/sessions/cao-demo/label", json={"label": "Run"})
        assert resp.status_code == 500
        assert "settings.json could not be read" in resp.json()["detail"]
        assert str(settings_file) not in resp.text
        assert settings_file.read_bytes() == before

    def test_clearing_over_an_unreadable_settings_file_is_a_500_not_a_silent_200(
        self, client, settings_file, backend
    ):
        """haofeif, #724 review, P3: a blank label used to suppress the
        unreadable-file error globally, so this endpoint answered 200 with
        ``label: null`` although no bytes changed, and the old label came back
        once the file was repaired. An explicit clear now surfaces it; only
        session teardown treats it as best-effort."""
        settings_file.write_text('{"session_labels": {"cao-demo": "Old"},}')  # trailing comma
        before = settings_file.read_bytes()
        resp = client.post("/sessions/cao-demo/label", json={"label": ""})
        assert resp.status_code == 500
        assert "settings.json could not be read" in resp.json()["detail"]
        assert settings_file.read_bytes() == before

    def test_list_and_get_surface_the_label(self, client, settings_file):
        settings_service.set_session_label("cao-demo", "Nightly triage")
        with patch("cli_agent_orchestrator.api.main.session_service") as svc:
            svc.list_sessions.return_value = [{"id": "cao-demo", "label": "Nightly triage"}]
            svc.get_session.return_value = {
                "session": {"id": "cao-demo", "label": "Nightly triage"},
                "terminals": [],
            }
            assert client.get("/sessions").json()[0]["label"] == "Nightly triage"
            assert client.get("/sessions/cao-demo").json()["session"]["label"] == "Nightly triage"


class TestSettingsWritersStayOffTheEventLoop:
    """plauzy, #724 review, blocking item 2: the label endpoint ran a tmux
    subprocess and a lock-polling settings write inline on the event loop, so
    with another writer holding the settings lock a 10 ms heartbeat task on
    the same loop ticked once in 1.8 s -- ``/health`` and every SSE stream
    froze with it. ``POST /settings/agent-dirs`` and ``/skill-dirs`` inherited
    the same wait once their writers went through ``_mutate``.

    Driven over ASGI directly (not the sync TestClient, which runs the app on
    its own portal thread and cannot observe the request loop) with a
    heartbeat coroutine on the SAME loop as the request. The settings lock is
    held by a plain thread for half a second; a handler that waits for it on
    the loop stalls the heartbeat, one that waits on a worker thread does not.
    """

    _WRITES = [
        pytest.param("/sessions/cao-demo/label", {"label": "Run"}, id="label"),
        pytest.param("/settings/agent-dirs", {"extra_dirs": ["/x"]}, id="agent-dirs"),
        pytest.param("/settings/skill-dirs", {"extra_dirs": ["/x"]}, id="skill-dirs"),
    ]

    @staticmethod
    def _hold_settings_lock(release: threading.Event, holding: threading.Event) -> None:
        from cli_agent_orchestrator.utils import atomic_file

        lock_path = atomic_file._lock_path_for(settings_service.SETTINGS_FILE)
        with atomic_file._file_lock(lock_path, timeout=5):
            holding.set()
            release.wait(5)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("url,body", _WRITES)
    async def test_a_held_settings_lock_does_not_freeze_the_loop(
        self, settings_file, backend, url, body
    ):
        from cli_agent_orchestrator.api.main import app
        from cli_agent_orchestrator.plugins import PluginRegistry

        release, holding = threading.Event(), threading.Event()
        holder = threading.Thread(target=self._hold_settings_lock, args=(release, holding))
        holder.start()
        assert holding.wait(5), "lock holder never started"

        ticks = 0

        async def heartbeat() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        pulse = asyncio.create_task(heartbeat())
        # Scheduled ON the loop: if the handler blocks the loop this never
        # fires, the holder times out after 5 s instead, and the tick count
        # exposes the stall either way.
        asyncio.get_running_loop().call_later(0.5, release.set)
        try:
            app.state.plugin_registry = PluginRegistry()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost"
            ) as client:
                resp = await client.post(url, json=body)
        finally:
            release.set()
            pulse.cancel()
            holder.join(5)
        assert resp.status_code == 200, resp.text
        assert ticks >= 20, f"the loop ticked {ticks} times while the writer waited for the lock"

    @pytest.mark.asyncio
    async def test_label_write_waits_for_a_teardown_holding_the_lifecycle_lock(
        self, settings_file, backend
    ):
        """haofeif, #724 review, P3: the existence probe used to run outside
        the per-session lifecycle lock, so a deletion could complete between
        the probe and the write and the request would persist an orphan label
        for the next same-name session to inherit. Probe and write now hold
        the lock, so they cannot interleave with ``delete_session``."""
        from cli_agent_orchestrator.api.main import app
        from cli_agent_orchestrator.plugins import PluginRegistry
        from cli_agent_orchestrator.services.session_lock import session_lifecycle_lock

        release, holding = threading.Event(), threading.Event()

        def teardown_in_progress() -> None:
            with session_lifecycle_lock("cao-demo"):
                holding.set()
                release.wait(5)

        holder = threading.Thread(target=teardown_in_progress)
        holder.start()
        assert holding.wait(5)
        try:
            app.state.plugin_registry = PluginRegistry()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost"
            ) as client:
                request = asyncio.create_task(
                    client.post("/sessions/cao-demo/label", json={"label": "Run"})
                )
                await asyncio.sleep(0.3)
                assert not request.done(), "the label write did not wait for the lifecycle lock"
                backend.session_exists.assert_not_called()
                release.set()
                resp = await asyncio.wait_for(request, 5)
        finally:
            release.set()
            holder.join(5)
        assert resp.status_code == 200, resp.text
        backend.session_exists.assert_called_once_with("cao-demo")
        assert settings_service.get_session_labels() == {"cao-demo": "Run"}


class TestWorkingDirectoryNormalizationAtTheBoundary:
    """Both creation endpoints accept operator-typed spellings and reject an
    unusable path with a clear 400 before any service is reached."""

    @staticmethod
    def _terminal(session="cao-demo"):
        return Terminal(
            id="abcd1234",
            name="test-window",
            session_name=session,
            provider="kiro_cli",
            agent_profile="developer",
        )

    def test_create_session_strips_quotes_and_forwards_the_normalized_path(self, client, tmp_path):
        with patch("cli_agent_orchestrator.api.main.session_service") as svc:
            svc.create_session = AsyncMock(return_value=self._terminal())
            resp = client.post(
                "/sessions",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "developer",
                    "working_directory": f'"{tmp_path}"',
                },
            )
        assert resp.status_code == 201
        assert svc.create_session.call_args.kwargs["working_directory"] == str(tmp_path)

    def test_create_session_rejects_a_relative_path_before_the_service(self, client):
        with patch("cli_agent_orchestrator.api.main.session_service") as svc:
            svc.create_session = AsyncMock()
            resp = client.post(
                "/sessions",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "developer",
                    "working_directory": "relative/not/absolute",
                },
            )
        assert resp.status_code == 400
        assert "absolute" in resp.json()["detail"].lower()
        svc.create_session.assert_not_called()

    def test_a_rejected_request_creates_no_directory(self, client, tmp_path):
        """The normalization can CREATE, so it runs after the cheap validations:
        a request some other check will reject must leave nothing behind."""
        target = tmp_path / "orphan" / "deep" / "tree"
        with patch("cli_agent_orchestrator.api.main.session_service") as svc:
            svc.create_session = AsyncMock()
            resp = client.post(
                "/sessions",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "developer",
                    "working_directory": str(target),
                    "session_name": "x" * 200,  # rejected by the name check
                },
            )
        assert resp.status_code == 400
        assert not target.exists(), "a rejected request left a directory tree behind"
        svc.create_session.assert_not_called()

    def test_create_session_asks_the_service_to_create_a_missing_folder(self, client, tmp_path):
        """The boundary creates NOTHING (plauzy, #724 review): it forwards the
        normalized path and the create flag, and terminal_service makes the
        directory at the point of use, after the checks that can 404/409/429."""
        target = tmp_path / "new" / "project"
        with patch("cli_agent_orchestrator.api.main.session_service") as svc:
            svc.create_session = AsyncMock(return_value=self._terminal())
            resp = client.post(
                "/sessions",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "developer",
                    "working_directory": str(target),
                },
            )
        assert resp.status_code == 201
        assert not target.exists(), "the HTTP boundary must not create directories"
        assert svc.create_session.call_args.kwargs["working_directory"] == str(target)
        assert svc.create_session.call_args.kwargs["create_working_directory"] is True

    def test_a_fictional_path_reaches_a_mocked_service_untouched(self, client):
        """The boundary is lexical only. Whether ``/workspace`` is allowed or
        exists is the service's call (made at the point of use, see
        ``test_working_directory_point_of_use.py``), so a caller that mocks the
        service -- every MCP handler test does, with exactly this path -- sees
        the mock's answer, not a policy 400 from the endpoint."""
        with (
            patch("cli_agent_orchestrator.api.main.terminal_service") as ts,
            patch("cli_agent_orchestrator.api.main.session_service") as svc,
        ):
            ts.create_terminal = AsyncMock(return_value=self._terminal())
            svc.create_session = AsyncMock(return_value=self._terminal())
            assert self._create(client, "terminal", "/workspace").status_code == 201
            assert self._create(client, "session", "/workspace").status_code == 201
        assert ts.create_terminal.call_args.kwargs["working_directory"] == "/workspace"
        assert svc.create_session.call_args.kwargs["working_directory"] == "/workspace"

    def test_create_session_maps_a_refused_directory_to_400(self, client, tmp_path):
        """The policy refusal (``/tmp/<new>``, ``/etc/<new>``) is raised by the
        service as WorkingDirectoryError; ``POST /sessions`` must answer 400
        with the policy's own wording, and the mocked service proves nothing
        was created on the way."""
        with patch("cli_agent_orchestrator.api.main.session_service") as svc:
            svc.create_session = AsyncMock(
                side_effect=WorkingDirectoryError(
                    "Working directory not allowed: /tmp/x (nearest existing ancestor "
                    "resolves to blocked system path /private/tmp)"
                )
            )
            resp = self._create(client, "session", str(tmp_path / "x"))
        assert resp.status_code == 400
        assert "not allowed" in resp.json()["detail"]

    @pytest.mark.parametrize("endpoint", ["session", "terminal"])
    def test_component_over_name_max_is_a_400_not_a_500(self, client, endpoint, tmp_path):
        """Under the 4096 total cap but over the OS's 255 per component:
        ``Path.exists()`` raised ``OSError(36)`` and both endpoints answered
        ``500 File name too long`` (plauzy, #724 review, blocking item 3)."""
        target = tmp_path / ("x" * 300)
        with (
            patch("cli_agent_orchestrator.api.main.terminal_service") as ts,
            patch("cli_agent_orchestrator.api.main.session_service") as svc,
        ):
            ts.create_terminal = AsyncMock()
            svc.create_session = AsyncMock()
            resp = self._create(client, endpoint, str(target))
        assert resp.status_code == 400, resp.json()
        assert "folder name longer than" in resp.json()["detail"]
        assert not os.path.exists(target)  # Path.exists itself raises ENAMETOOLONG
        ts.create_terminal.assert_not_called()
        svc.create_session.assert_not_called()

    @pytest.mark.parametrize("endpoint", ["session", "terminal"])
    def test_windows_path_that_leaves_its_drive_is_a_400(self, client, endpoint, tmp_path):
        mnt = tmp_path / "mnt"
        (mnt / "c").mkdir(parents=True)
        with (
            patch("cli_agent_orchestrator.utils.paths._mounted_drives", return_value={"c"}),
            patch("cli_agent_orchestrator.api.main.terminal_service") as ts,
            patch("cli_agent_orchestrator.api.main.session_service") as svc,
        ):
            ts.create_terminal = AsyncMock()
            svc.create_session = AsyncMock()
            resp = self._create(client, endpoint, r"C:\..\..\Windows")
        assert resp.status_code == 400, resp.json()
        assert "leaves drive C:" in resp.json()["detail"]
        ts.create_terminal.assert_not_called()
        svc.create_session.assert_not_called()

    def test_working_directory_error_from_the_terminal_service_is_a_400_not_a_404(
        self, client, tmp_path
    ):
        """The point-of-use mkdir can still fail (EACCES on a parent, a
        read-only filesystem). ``POST /sessions/{name}/terminals`` maps a bare
        ValueError to 404 "session not found"; a refused directory must come
        out as the 400 it is."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as ts:
            ts.create_terminal = AsyncMock(
                side_effect=WorkingDirectoryError("Folder '/x' could not be created: EACCES")
            )
            resp = self._create(client, "terminal", str(tmp_path))
        assert resp.status_code == 400
        assert "could not be created" in resp.json()["detail"]

    @staticmethod
    def _create(client, endpoint, working_directory):
        params = {
            "provider": "kiro_cli",
            "agent_profile": "developer",
            "working_directory": working_directory,
        }
        if endpoint == "session":
            return client.post("/sessions", params=params)
        return client.post("/sessions/cao-demo/terminals", params=params)

    def test_create_terminal_in_session_normalizes_too(self, client, tmp_path):
        with (
            patch("cli_agent_orchestrator.api.main.terminal_service") as ts,
            patch("cli_agent_orchestrator.api.main.session_service") as svc,
        ):
            svc.get_session.return_value = {"session": {"id": "cao-demo"}, "terminals": []}
            ts.create_terminal = AsyncMock(return_value=self._terminal())
            resp = client.post(
                "/sessions/cao-demo/terminals",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "developer",
                    "working_directory": f'"{tmp_path}"',
                },
            )
        assert resp.status_code == 201, resp.json()
        assert ts.create_terminal.call_args.kwargs["working_directory"] == str(tmp_path)
        assert ts.create_terminal.call_args.kwargs["create_working_directory"] is True

    def test_create_terminal_rejected_request_creates_no_directory(self, client, tmp_path):
        """Same rule as ``POST /sessions``: the normalization can CREATE, so a
        request another validation rejects must leave nothing behind. Here the
        rejection is the initial_message-without-defer_init guard."""
        target = tmp_path / "orphan" / "deep" / "tree"
        with (
            patch("cli_agent_orchestrator.api.main.terminal_service") as ts,
            patch("cli_agent_orchestrator.api.main.session_service") as svc,
        ):
            svc.get_session.return_value = {"session": {"id": "cao-demo"}, "terminals": []}
            ts.create_terminal = AsyncMock()
            resp = client.post(
                "/sessions/cao-demo/terminals",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "developer",
                    "working_directory": str(target),
                },
                json={"initial_message": "hello"},  # rejected: defer_init is false
            )
        assert resp.status_code == 400
        assert "defer_init" in resp.json()["detail"]
        assert not target.exists(), "a rejected request left a directory tree behind"
        ts.create_terminal.assert_not_called()

    def test_create_terminal_in_session_rejects_a_relative_path(self, client):
        with (
            patch("cli_agent_orchestrator.api.main.terminal_service") as ts,
            patch("cli_agent_orchestrator.api.main.session_service") as svc,
        ):
            svc.get_session.return_value = {"session": {"id": "cao-demo"}, "terminals": []}
            ts.create_terminal = AsyncMock()
            resp = client.post(
                "/sessions/cao-demo/terminals",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "developer",
                    "working_directory": "relative/not/absolute",
                },
            )
        assert resp.status_code == 400
        assert "absolute" in resp.json()["detail"].lower()
        ts.create_terminal.assert_not_called()
