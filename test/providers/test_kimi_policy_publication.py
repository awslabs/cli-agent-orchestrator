"""A failed hard-tool-policy rewrite must never publish a broader runtime config."""

import os

import pytest

from cli_agent_orchestrator.providers import kimi_runtime_home as runtime


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('model = "keep-model"\n[tools]\nenabled = ["Read", "Bash"]\n')
    return path


@pytest.mark.parametrize(
    "tools", ['tools = "Read"', '[tools]\nenabled = "Read"', '[tools]\nenabled = ["Read", 1]']
)
def test_invalid_existing_policy_is_rejected_without_rewriting(tmp_path, tools):
    config = tmp_path / "config.toml"
    config.write_text(tools + "\n")
    original = config.read_bytes()

    with pytest.raises(runtime.RuntimeHomeError, match="must be"):
        runtime.KimiCodeRuntimeHomeBuilder._apply_tool_allowlist(config, ["Read"])

    assert config.read_bytes() == original
    assert not config.with_name("config.toml.tools.tmp").exists()


@pytest.mark.parametrize(
    "corruption", ['model = "changed"\n[tools]\nenabled = ["Read"]\n', 'model = "unterminated']
)
def test_semantic_verification_rejects_corrupt_round_trip(config, monkeypatch, corruption):
    original = config.read_bytes()
    monkeypatch.setattr(runtime.tomlkit, "dumps", lambda document: corruption)

    with pytest.raises(runtime.RuntimeHomeError, match="Refusing to write"):
        runtime.KimiCodeRuntimeHomeBuilder._apply_tool_allowlist(config, ["Read"])

    assert config.read_bytes() == original
    assert not config.with_name("config.toml.tools.tmp").exists()


@pytest.mark.parametrize("persisted", [b'model = "keep-model"\n', b'model = "unterminated'])
def test_verified_memory_document_does_not_excuse_corrupt_disk_bytes(
    config, monkeypatch, persisted
):
    original = config.read_bytes()
    real_write = os.write

    def lying_write(fd, payload):
        real_write(fd, persisted)
        return len(payload)

    monkeypatch.setattr(runtime.os, "write", lying_write)
    with pytest.raises(runtime.RuntimeHomeError, match="Refusing to publish"):
        runtime.KimiCodeRuntimeHomeBuilder._apply_tool_allowlist(config, ["Read"])

    assert config.read_bytes() == original
    assert not config.with_name("config.toml.tools.tmp").exists()


@pytest.mark.parametrize("failed_operation", ["fsync", "replace"])
def test_io_failure_keeps_prior_config_and_removes_unpublished_policy(
    config, monkeypatch, failed_operation
):
    original = config.read_bytes()

    def fail(*args):
        raise OSError("injected storage failure")

    monkeypatch.setattr(runtime.os, failed_operation, fail)
    with pytest.raises(runtime.RuntimeHomeError, match="Could not write"):
        runtime.KimiCodeRuntimeHomeBuilder._apply_tool_allowlist(config, ["Read"])

    assert config.read_bytes() == original
    assert not config.with_name("config.toml.tools.tmp").exists()


@pytest.mark.parametrize(
    "existing,requested,expected",
    [
        (["*"], ["Read"], ["Read"]),
        (["Read"], ["*", "Grep"], ["Read"]),
        (["mcp__bridge__read"], ["mcp__bridge__*"], ["mcp__bridge__read"]),
        (["mcp__bridge__read*"], ["mcp__bridge__*"], ["mcp__bridge__read*"]),
        (["mcp__bridge__*"], ["mcp__bridge__read*"], ["mcp__bridge__read*"]),
    ],
)
def test_published_policy_intersects_existing_and_requested_grants(
    tmp_path, existing, requested, expected
):
    source = tmp_path / "source"
    source.mkdir()
    original = f'model = "keep-model"\n[tools]\nenabled = {existing!r}\n'
    (source / "config.toml").write_text(original)

    result = runtime.KimiCodeRuntimeHomeBuilder(source, tmp_path / "worker").build(
        tool_allowlist=requested
    )
    published = result.home / "config.toml"
    parsed = runtime.tomllib.loads(published.read_text())

    assert parsed == {"model": "keep-model", "tools": {"enabled": expected}}
    assert published.stat().st_mode & 0o777 == 0o600
    assert (source / "config.toml").read_text() == original
    assert not published.with_name("config.toml.tools.tmp").exists()
