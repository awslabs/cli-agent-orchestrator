"""Exercise session status through the real HTTP read routes and SQLite rows."""

import json
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.cli.commands.session import session
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import session_service, terminal_service
from cli_agent_orchestrator.utils import api_http


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("selection", ["workers", "conductor", "terminal", "empty"])
def test_status_reads_worker_details(tmp_path, monkeypatch, as_json, selection):
    engine = create_engine(f"sqlite:///{tmp_path / 'terminals.db'}")
    database.Base.metadata.create_all(engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(terminal_service, "TERMINAL_LOG_DIR", tmp_path)
    backend = Mock()
    backend.session_exists_strict.return_value = True
    monkeypatch.setattr(session_service, "get_backend", lambda: backend)
    states = {
        "f0000000": TerminalStatus.IDLE,
        "10000000": TerminalStatus.PROCESSING,
        "20000000": TerminalStatus.COMPLETED,
    }
    ids = list(states)[:1] if selection == "empty" else list(states)
    for terminal_id in ids:
        database.create_terminal(
            terminal_id, "cao-test", terminal_id, "kiro_cli", agent_profile="developer"
        )
    monkeypatch.setattr(terminal_service.status_monitor, "get_status", states.__getitem__)
    # LAST extraction is provider-specific; the regression concerns status reads.
    monkeypatch.setattr(terminal_service, "get_output", lambda *args: "response")
    client = TestClient(app, base_url="http://localhost")
    reads = []

    def get(url, **kwargs):
        path = urlsplit(url).path
        reads.append(path)
        return client.get(path, **kwargs)

    monkeypatch.setattr(api_http, "get", get)
    before = database.list_terminals_by_session("cao-test")
    assert all("status" not in row for row in before)
    args = ["status", "cao-test"]
    if selection != "conductor":
        args.append("--workers")
    if selection == "terminal":
        args.extend(["--terminal", "20000000"])
    if as_json:
        args.append("--json")
    try:
        result = CliRunner().invoke(session, args)
        assert result.exit_code == 0, result.output
        if selection == "workers":
            expected = [
                client.get(f"/terminals/{terminal_id}").json()["status"] for terminal_id in ids[1:]
            ]
            if as_json:
                data = json.loads(result.output)
                assert data["conductor"]["id"] == ids[0]
                assert [row["id"] for row in data["workers"]] == ids[1:]
                assert [row["status"] for row in data["workers"]] == expected
            else:
                for terminal_id, status in zip(ids[1:], expected):
                    row = next(line for line in result.output.splitlines() if terminal_id in line)
                    assert status in row
        elif as_json:
            data = json.loads(result.output)
            if selection == "empty":
                assert data["workers"] == []
            else:
                assert "workers" not in data
        elif selection == "empty":
            assert "No worker terminals" in result.output
        if selection in ("conductor", "empty"):
            assert "/terminals/10000000" not in reads
        elif selection == "terminal":
            assert reads == ["/terminals/20000000", "/terminals/20000000/output"]
        assert database.list_terminals_by_session("cao-test") == before
        assert backend.method_calls == (
            [] if selection == "terminal" else [("session_exists_strict", ("cao-test",), {})]
        )
    finally:
        client.close()
        engine.dispose()
