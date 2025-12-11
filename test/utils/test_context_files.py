from cli_agent_orchestrator.utils import context_files as ctx


def test_write_and_get_context_provider(tmp_path, monkeypatch):
    source = tmp_path / "src.md"
    source.write_text("---\nname: developer\n---\ncontent\n")

    dest_dir = tmp_path / "agent-context"
    monkeypatch.setattr(ctx, "AGENT_CONTEXT_DIR", dest_dir)

    dest = dest_dir / "developer.md"
    dest_dir.mkdir(parents=True, exist_ok=True)

    ctx.write_context_with_provider(source, "droid", dest)

    assert dest.exists()
    assert ctx.get_context_provider("developer") == "droid"


def test_get_context_provider_missing(tmp_path, monkeypatch):
    dest_dir = tmp_path / "agent-context"
    monkeypatch.setattr(ctx, "AGENT_CONTEXT_DIR", dest_dir)

    assert ctx.get_context_provider("missing") is None
