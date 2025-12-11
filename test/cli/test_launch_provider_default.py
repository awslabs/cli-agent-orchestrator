from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.launch import launch


def test_launch_uses_context_provider(monkeypatch):
    runner = CliRunner()
    calls = {}

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"session_name": "cao-session", "name": "window-0"}

    def fake_post(url, params):
        calls["url"] = url
        calls["params"] = params
        return DummyResponse()

    monkeypatch.setattr(
        "cli_agent_orchestrator.cli.commands.launch.requests.post", fake_post
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.cli.commands.launch.subprocess.run", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.cli.commands.launch.get_context_provider", lambda agent: "codex"
    )

    result = runner.invoke(launch, ["--agents", "developer", "--headless"])

    assert result.exit_code == 0
    assert calls["params"]["provider"] == "codex"
