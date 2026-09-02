"""Operator-facing ergonomics on the HTTP API: working-directory
normalization at the session/terminal creation boundary, the server-side
folder listing, per-session display labels, and the ``X-Server-Time`` header.
"""

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
        assert "is a file" in resp.json()["detail"]

    def test_defaults_to_home(self, client):
        resp = client.get("/fs/dirs")
        assert resp.status_code == 200
        assert resp.json()["path"] == str(Path.home().resolve())

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
    def test_label_roundtrip_and_clear(self, client, settings_file):
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

    def test_label_is_required(self, client, settings_file):
        resp = client.post("/sessions/cao-demo/label", json={})
        assert resp.status_code == 422

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
