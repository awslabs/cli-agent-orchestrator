"""Operator-facing ergonomics on the HTTP API: working-directory
normalization at the session/terminal creation boundary, the server-side
folder listing, per-session display labels, and the ``X-Server-Time`` header.
"""

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import Terminal
from cli_agent_orchestrator.services import settings_service


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

    def test_create_session_creates_a_missing_folder(self, client, tmp_path):
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
        assert target.is_dir()
        assert svc.create_session.call_args.kwargs["working_directory"] == str(target)

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
