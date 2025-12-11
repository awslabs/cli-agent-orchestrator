from fastapi.testclient import TestClient

from cli_agent_orchestrator.api import main
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.terminal import Terminal
from cli_agent_orchestrator.utils import context_files as ctx


def test_create_session_defaults_provider_from_context(tmp_path, monkeypatch):
    monkeypatch.setattr(ctx, "AGENT_CONTEXT_DIR", tmp_path)
    source = tmp_path / "source.md"
    source.write_text("---\nname: developer\n---\ncontent\n")
    dest = tmp_path / "developer.md"
    ctx.write_context_with_provider(source, "codex", dest)

    def fake_create_terminal(provider, agent_profile, session_name=None, new_session=True):
        assert provider == "codex"
        return Terminal(
            id="deadbeef",
            name="window-0",
            provider=ProviderType(provider),
            session_name=session_name or "cao-session",
            agent_profile=agent_profile,
        )

    monkeypatch.setattr(main.terminal_service, "create_terminal", fake_create_terminal)

    client = TestClient(main.app)
    response = client.post("/sessions", params={"agent_profile": "developer"})

    assert response.status_code == 201
    body = response.json()
    assert body["provider"] == "codex"


def test_create_session_uses_default_when_missing_context(monkeypatch):
    monkeypatch.setattr(main, "get_context_provider", lambda agent: None)

    def fake_create_terminal(provider, agent_profile, session_name=None, new_session=True):
        assert provider == "q_cli"
        return Terminal(
            id="deadbeef",
            name="window-0",
            provider=ProviderType(provider),
            session_name=session_name or "cao-session",
            agent_profile=agent_profile,
        )

    monkeypatch.setattr(main.terminal_service, "create_terminal", fake_create_terminal)

    client = TestClient(main.app)
    response = client.post("/sessions", params={"agent_profile": "developer"})

    assert response.status_code == 201
    assert response.json()["provider"] == "q_cli"
