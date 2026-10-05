"""Provider overrides must preserve unresolved profile source at the install boundary."""

import json
from string import Template

import frontmatter
import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.install import install
from cli_agent_orchestrator.services import install_service
from cli_agent_orchestrator.utils import env


@pytest.fixture(autouse=True)
def fake_env(monkeypatch):
    """Resolve fake values without reading or writing a managed env file."""
    values = {"TOKEN": "fake-token"}
    monkeypatch.setattr(env, "load_env_vars", lambda: values)
    monkeypatch.setattr(install_service, "load_env_vars", lambda: values)


@pytest.mark.parametrize("shape", ["mapping", "sequence"])
@pytest.mark.parametrize("has_recorded", [False, True])
@pytest.mark.parametrize("selected", ["claude_code", "kiro_cli"])
def test_provider_override_with_flow_placeholders(
    workspace, tmp_path, shape, has_recorded, selected
):
    recorded = (
        ("kiro_cli" if selected == "claude_code" else "claude_code") if has_recorded else None
    )
    field = "env: {TOKEN: ${TOKEN}}" if shape == "mapping" else "args: [--token, ${TOKEN}]"
    provider_line = f"provider: {recorded}\n" if recorded else ""
    raw = (
        "---\n# Keep this comment and field order.\n"
        "name: worker\ndescription: 'Flow placeholders'\n"
        f"{provider_line}"
        "mcpServers:\n  helper:\n    command: fake-server\n"
        f"    {field}\n---\nTask with ${{TOKEN}}.\nprovider: body-only\n"
    )
    source = tmp_path / "worker.md"
    source.write_text(raw, encoding="utf-8")

    # Ordinary installation with a matching provider already accepts the profile.
    if recorded:
        normal = CliRunner().invoke(install, [str(source)])
        assert "installed successfully" in normal.output, normal.output

    result = CliRunner().invoke(install, [str(source), "--provider", selected])
    assert result.exit_code == 0, result.output
    assert "installed successfully" in result.output, result.output
    stored = (workspace["local_store"] / "worker.md").read_text(encoding="utf-8")
    parsed = frontmatter.loads(Template(stored).safe_substitute(TOKEN="fake-token"))
    assert parsed["provider"] == selected
    assert parsed["mcpServers"]["helper"] == (
        {"command": "fake-server", "env": {"TOKEN": "fake-token"}}
        if shape == "mapping"
        else {"command": "fake-server", "args": ["--token", "fake-token"]}
    )
    assert "${TOKEN}" in stored
    assert "fake-token" not in stored
    context = (workspace["context_dir"] / "worker.md").read_text(encoding="utf-8")
    assert "${TOKEN}" in context
    assert "fake-token" not in context
    assert context.endswith(raw)
    if selected == "kiro_cli":
        artifact = json.loads(
            (workspace["kiro_agents_dir"] / "worker.json").read_text(encoding="utf-8")
        )
        helper = artifact["mcpServers"]["helper"]
        assert helper["command"] == "fake-server"
        if shape == "mapping":
            assert helper["env"]["TOKEN"] == "fake-token"
        else:
            assert helper["args"] == ["--token", "fake-token"]
    reinstall = CliRunner().invoke(install, ["worker"])
    assert "installed successfully" in reinstall.output, reinstall.output
    assert source.read_text(encoding="utf-8") == raw
    if recorded:
        assert stored == raw.replace(provider_line, f"provider: {selected}\n")
    else:
        assert stored.replace(f"provider: {selected}\n", "", 1) == raw


@pytest.mark.parametrize(
    "metadata",
    [
        "name: worker\n'provider': kiro_cli # retain comment\n",
        "name: worker\nprovider: >-\n  kiro_cli\n",
        "name: worker\nprovider: kiro_cli\nprovider: codex\n",
        "name: worker\nextra:\n  provider: keep\n",
        "{name: worker, provider: kiro_cli, description: 'keep'}\n",
        "{name: worker, description: 'keep'}\n",
        "{name: worker, description: 'keep',}\n",
        "name: worker\nprovider:\n",
        "name: worker\nprovider: !!str kiro_cli\n",
        "name: worker\nprovider: !!null null\n",
        "name: worker\nprovider: !!null\n",
        "name: worker\nextra: &original kiro_cli\nprovider: *original\n",
        "name: worker\nprovider: &original kiro_cli\nextra: *original\n",
    ],
)
def test_provider_override_preserves_other_metadata(workspace, tmp_path, metadata):
    raw = f"---\n{metadata}---\nTask.\n"
    source = tmp_path / "worker.md"
    source.write_text(raw, encoding="utf-8")
    result = CliRunner().invoke(install, [str(source), "--provider", "claude_code"])
    assert "installed successfully" in result.output, result.output
    stored = (workspace["local_store"] / "worker.md").read_text(encoding="utf-8")
    original = frontmatter.loads(raw)
    parsed = frontmatter.loads(stored)
    assert parsed["provider"] == "claude_code"
    assert parsed.content == original.content
    assert {key: value for key, value in parsed.metadata.items() if key != "provider"} == {
        key: value for key, value in original.metadata.items() if key != "provider"
    }
    if "# retain comment" in raw:
        assert "# retain comment" in stored


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_provider_recording_retains_delimiters_and_indentation(workspace, tmp_path, newline):
    raw = newline.join(["----", "  name: worker", "  description: Test", "----", "Task.", ""])
    source = tmp_path / "worker.md"
    source.write_bytes(raw.encode("utf-8"))
    # Exercise the service's byte-preserving source input (Path.read_text in the
    # CLI normalizes CRLF before the service sees it).
    result = install_service.install_agent("worker", "claude_code", profile_content=raw)
    assert result.success, result.message
    stored = (workspace["local_store"] / "worker.md").read_bytes().decode("utf-8")
    assert stored.replace("  provider: claude_code" + newline, "", 1) == raw


def test_provider_recording_without_frontmatter(workspace, tmp_path):
    source = tmp_path / "worker.md"
    raw = "Task without metadata.\n"
    source.write_text(raw, encoding="utf-8")
    result = CliRunner().invoke(install, [str(source), "--provider", "claude_code"])
    assert "installed successfully" in result.output, result.output
    stored = (workspace["local_store"] / "worker.md").read_text(encoding="utf-8")
    assert stored == "---\nprovider: claude_code\n---\n" + raw


@pytest.mark.parametrize("shape", ["mapping", "sequence"])
@pytest.mark.parametrize("preserve", [False, True])
def test_matching_or_preserved_provider_keeps_raw_store(workspace, tmp_path, shape, preserve):
    field = "env: {TOKEN: ${TOKEN}}" if shape == "mapping" else "args: [${TOKEN}]"
    raw = (
        "---\nname: worker\nprovider: claude_code\n"
        f"mcpServers:\n  helper:\n    command: fake-server\n    {field}\n---\nTask.\n"
    )
    result = install_service.install_agent(
        "worker",
        "kiro_cli" if preserve else "claude_code",
        preserve_recorded_provider=preserve,
        profile_content=raw,
    )
    assert result.success, result.message
    stored = (workspace["local_store"] / "worker.md").read_text(encoding="utf-8")
    assert stored == raw
    assert result.provider == ("kiro_cli" if preserve else "claude_code")
    context = (workspace["context_dir"] / "worker.md").read_text(encoding="utf-8")
    assert "${TOKEN}" in context
    assert "fake-token" not in context
    if preserve:
        artifact = json.loads(
            (workspace["kiro_agents_dir"] / "worker.json").read_text(encoding="utf-8")
        )
        helper = artifact["mcpServers"]["helper"]
        if shape == "mapping":
            assert helper["env"]["TOKEN"] == "fake-token"
        else:
            assert helper["args"] == ["fake-token"]


def test_provider_recording_flow_trailing_comment():
    # The existing context provenance writer refuses this form, independently
    # of provider recording. Keep its behavior out of this issue's change.
    raw = "---\n{name: worker} # keep\n---\nTask.\n"
    stored = install_service._content_with_recorded_provider(raw, "claude_code")
    assert stored == "---\n{name: worker, provider: claude_code} # keep\n---\nTask.\n"


@pytest.mark.parametrize("shape", ["mapping", "sequence"])
def test_api_provider_override_preserves_raw_and_resolves_artifact(workspace, shape):
    from fastapi.testclient import TestClient

    from cli_agent_orchestrator.api.main import app
    from cli_agent_orchestrator.utils.agent_profiles import resolve_provider

    field = "env: {TOKEN: ${TOKEN}}" if shape == "mapping" else "args: [--token, ${TOKEN}]"
    raw = (
        "---\n# Preserve this comment.\nname: api-worker\ndescription: 'Keep quoting'\n"
        f"mcpServers:\n  helper:\n    command: fake-server\n    {field}\n"
        "---\nBody ${TOKEN}.\nprovider: body-only\n"
    )
    path = workspace["local_store"] / "api-worker.md"
    path.write_text(raw, encoding="utf-8")
    response = TestClient(app, base_url="http://localhost").post(
        "/agents/profiles/install", json={"source": "api-worker", "provider": "kiro_cli"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["provider"] == "kiro_cli"
    stored = path.read_text(encoding="utf-8")
    assert stored.replace("provider: kiro_cli\n", "", 1) == raw
    assert "fake-token" not in stored
    context = (workspace["context_dir"] / "api-worker.md").read_text(encoding="utf-8")
    assert context.endswith(raw)
    assert "fake-token" not in context
    assert resolve_provider("api-worker", fallback_provider="claude_code") == "kiro_cli"
    artifact = json.loads(
        (workspace["kiro_agents_dir"] / "api-worker.json").read_text(encoding="utf-8")
    )
    helper = artifact["mcpServers"]["helper"]
    if shape == "mapping":
        assert helper["env"]["TOKEN"] == "fake-token"
    else:
        assert helper["args"] == ["--token", "fake-token"]


def test_invalid_explicit_provider_fails_before_profile_or_env_access(workspace, monkeypatch):
    from unittest.mock import Mock

    seams = []
    for name in (
        "_download_agent",
        "_read_agent_profile_source",
        "load_env_vars",
        "set_env_var",
        "write_profile",
    ):
        seam = Mock(side_effect=AssertionError(f"Unexpected {name} access"))
        monkeypatch.setattr(install_service, name, seam)
        seams.append(seam)
    result = install_service.install_agent(
        "https://raw.githubusercontent.com/example/repo/main/worker.md",
        "invalid-provider",
        env_vars={"TOKEN": "fake-token"},
    )
    assert not result.success
    assert result.message.startswith("Invalid provider 'invalid-provider'.")
    for seam in seams:
        seam.assert_not_called()
    assert list(workspace["local_store"].iterdir()) == []
    assert list(workspace["context_dir"].iterdir()) == []


@pytest.mark.parametrize("provider_line", ["provider:\n", "provider: # todo\n"])
@pytest.mark.parametrize("shape", ["mapping", "sequence"])
def test_empty_provider_before_other_keys_with_flow_placeholder(
    workspace, tmp_path, provider_line, shape
):
    field = "env: {TOKEN: ${TOKEN}}" if shape == "mapping" else "args: [${TOKEN}]"
    raw = (
        f"---\n{provider_line}name: worker\nmcpServers:\n  helper:\n"
        f"    command: fake-server\n    {field}\n---\nTask.\n"
    )
    source = tmp_path / "worker.md"
    source.write_text(raw, encoding="utf-8")
    result = CliRunner().invoke(install, [str(source), "--provider", "claude_code"])
    assert "installed successfully" in result.output, result.output
    stored = (workspace["local_store"] / "worker.md").read_text(encoding="utf-8")
    expected = raw.replace("provider:", "provider: claude_code", 1)
    assert stored == expected
    assert (
        frontmatter.loads(Template(stored).safe_substitute(TOKEN="fake-token"))["provider"]
        == "claude_code"
    )


def test_indented_delimiter_in_block_scalar_keeps_metadata_and_raw_bytes(workspace):
    raw = (
        "---\nname: worker\ndescription: |\n  before\n  ---\n  after\n"
        "mcpServers:\n  helper:\n    command: fake-server\n"
        "    env: {TOKEN: '${TOKEN}'}\n---\nBody.\n"
    )
    result = install_service.install_agent("worker", "claude_code", profile_content=raw)
    assert result.success, result.message
    stored = (workspace["local_store"] / "worker.md").read_text(encoding="utf-8")
    assert stored == raw.replace("\n---\nBody.", "\nprovider: claude_code\n---\nBody.")
    assert frontmatter.loads(stored)["provider"] == "claude_code"
    assert frontmatter.loads(stored)["description"] == frontmatter.loads(raw)["description"]


def test_bom_does_not_silently_lose_recorded_provider(workspace):
    raw = "\ufeff---\nname: worker\ndescription: Test\n---\nTask ${TOKEN}.\n"
    result = install_service.install_agent("worker", "claude_code", profile_content=raw)
    assert result.success, result.message
    stored = (workspace["local_store"] / "worker.md").read_text(encoding="utf-8")
    parsed = frontmatter.loads(Template(stored).safe_substitute(TOKEN="fake-token"))
    original = frontmatter.loads(Template(raw).safe_substitute(TOKEN="fake-token"))
    assert parsed.metadata == {**original.metadata, "provider": "claude_code"}
    assert parsed.content == original.content
    assert "fake-token" not in stored


@pytest.mark.parametrize("raw_parseable", [False, True])
@pytest.mark.parametrize("candidate_kind", ["malformed", "metadata", "body"])
def test_recording_readback_fallback_or_refusal_precedes_all_writes(
    workspace, monkeypatch, raw_parseable, candidate_kind
):
    from unittest.mock import Mock

    field = "env: {TOKEN: '${TOKEN}'}" if raw_parseable else "env: {TOKEN: ${TOKEN}}"
    raw = (
        "---\nname: worker\n# Keep raw secrets out.\nmcpServers:\n  helper:\n"
        f"    command: fake-server\n    {field}\n---\nTask ${{TOKEN}}.\n"
    )
    loader = Mock(return_value={"TOKEN": "fake-secret-token"})
    persist = Mock()
    store = Mock(wraps=install_service.write_profile)
    context = Mock(wraps=install_service._write_context_file)
    monkeypatch.setattr(install_service, "load_env_vars", loader)
    monkeypatch.setattr(install_service, "set_env_var", persist)
    monkeypatch.setattr(install_service, "write_profile", store)
    monkeypatch.setattr(install_service, "_write_context_file", context)
    candidates = {
        "malformed": "---\nprovider: claude_code\ninvalid: {token: ${TOKEN}\n---\nTask.\n",
        "metadata": "---\nname: worker\nprovider: claude_code\n---\nTask ${TOKEN}.\n",
        "body": raw.replace("\n---\nTask", "\nprovider: claude_code\n---\nChanged"),
    }
    monkeypatch.setattr(
        install_service, "_content_with_recorded_provider", lambda *args: candidates[candidate_kind]
    )
    result = install_service.install_agent(
        "worker", "claude_code", env_vars={"TOKEN": "fake-secret-token"}, profile_content=raw
    )
    assert result.success is raw_parseable, result.message
    loader.assert_called_once()
    if raw_parseable:
        stored = (workspace["local_store"] / "worker.md").read_text(encoding="utf-8")
        assert "${TOKEN}" in stored
        assert "fake-secret-token" not in stored
        assert frontmatter.loads(stored)["provider"] == "claude_code"
        persist.assert_called_once_with("TOKEN", "fake-secret-token")
    else:
        assert "fake-secret-token" not in result.message
        assert "Could not safely record" in result.message
        persist.assert_not_called()
        store.assert_not_called()
        context.assert_not_called()
        assert list(workspace["local_store"].iterdir()) == []
        assert list(workspace["context_dir"].iterdir()) == []


@pytest.mark.parametrize("token", ["${TOKEN}", "fake-secret-token"])
def test_invalid_resolved_yaml_error_does_not_expose_secret(workspace, monkeypatch, token):
    monkeypatch.setattr(install_service, "load_env_vars", lambda: {"TOKEN": "fake-secret-token"})
    raw = "---\nname: worker\nmcpServers: {bad: [" + token + "}\n---\nTask.\n"
    result = install_service.install_agent(
        "worker", "claude_code", env_vars={"TOKEN": "fake-secret-token"}, profile_content=raw
    )
    assert not result.success
    assert "fake-secret-token" not in result.message
    assert "Could not parse profile after environment substitution" in result.message
    assert "line 3, column 37" in result.message
    assert "unresolved template variables" in result.message
    assert "mcpServers:" not in result.message
    assert list(workspace["local_store"].iterdir()) == []


def test_recording_readback_uses_fresh_metadata_after_plugin_mutation(workspace, monkeypatch):
    from unittest.mock import Mock

    original_apply = install_service.apply_plugin_mcp_servers

    def mutate_profile(profile, **kwargs):
        result = original_apply(profile, **kwargs)
        profile.description = "Mutated by delivery"
        return result

    loader = Mock(return_value={"TOKEN": "fake-token"})
    monkeypatch.setattr(install_service, "load_env_vars", loader)
    monkeypatch.setattr(install_service, "apply_plugin_mcp_servers", mutate_profile)
    raw = (
        "---\nname: worker\ndescription: Original\n"
        "mcpServers:\n  helper:\n    command: fake-server\n    env: {TOKEN: ${TOKEN}}\n"
        "---\nTask.\n"
    )
    result = install_service.install_agent("worker", "claude_code", profile_content=raw)
    assert result.success, result.message
    loader.assert_called_once()
    stored = (workspace["local_store"] / "worker.md").read_text(encoding="utf-8")
    assert stored.replace("provider: claude_code\n", "", 1) == raw


def test_structural_template_refuses_instead_of_losing_metadata(workspace, monkeypatch):
    from unittest.mock import Mock

    loader = Mock(
        return_value={"METADATA": "{name: worker, description: Expanded}", "TOKEN": "fake-token"}
    )
    persist = Mock()
    monkeypatch.setattr(install_service, "load_env_vars", loader)
    monkeypatch.setattr(install_service, "set_env_var", persist)
    raw = "---\n${METADATA}\n---\nTask ${TOKEN}.\n"
    result = install_service.install_agent(
        "worker", "claude_code", env_vars={"TOKEN": "fake-token"}, profile_content=raw
    )
    assert not result.success
    assert "Could not safely record" in result.message
    assert "fake-token" not in result.message
    loader.assert_called_once()
    persist.assert_not_called()
    assert list(workspace["local_store"].iterdir()) == []
    assert list(workspace["context_dir"].iterdir()) == []


@pytest.mark.parametrize("metadata", ["{name: worker, provider}\n", "? provider\nname: worker\n"])
@pytest.mark.parametrize("surface", ["service", "cli"])
def test_colonless_provider_key_uses_verified_legacy_fallback(
    workspace, tmp_path, metadata, surface
):
    from cli_agent_orchestrator.utils.agent_profiles import resolve_provider

    raw = f"---\n{metadata}---\nTask with ${{TOKEN}}.\nprovider: body-only\n"
    if surface == "service":
        result = install_service.install_agent("worker", "claude_code", profile_content=raw)
        assert result.success, result.message
        assert result.provider == "claude_code"
    else:
        source = tmp_path / "worker.md"
        source.write_text(raw, encoding="utf-8")
        result = CliRunner().invoke(install, [str(source), "--provider", "claude_code"])
        assert result.exit_code == 0, result.output
        assert "installed successfully" in result.output, result.output
        assert source.read_text(encoding="utf-8") == raw

    stored = (workspace["local_store"] / "worker.md").read_text(encoding="utf-8")
    assert "${TOKEN}" in stored
    assert "fake-token" not in stored
    actual = frontmatter.loads(Template(stored).safe_substitute(TOKEN="fake-token"))
    expected = frontmatter.loads(Template(raw).safe_substitute(TOKEN="fake-token"))
    assert actual.metadata == {**expected.metadata, "provider": "claude_code"}
    assert actual.content == expected.content
    context = (workspace["context_dir"] / "worker.md").read_text(encoding="utf-8")
    assert "${TOKEN}" in context
    assert "fake-token" not in context
    assert resolve_provider("worker", fallback_provider="kiro_cli") == "claude_code"


def test_yaml_error_without_problem_mark_keeps_generic_safe_hint(workspace, monkeypatch):
    from unittest.mock import Mock

    import yaml

    monkeypatch.setattr(
        install_service,
        "parse_agent_profile_text",
        Mock(side_effect=yaml.YAMLError("fake-secret-token")),
    )
    result = install_service.install_agent(
        "worker", "claude_code", profile_content="---\nname: worker\n---\nTask.\n"
    )
    assert not result.success
    assert "unresolved template variables" in result.message
    assert "fake-secret-token" not in result.message
    assert " at line " not in result.message
    assert list(workspace["local_store"].iterdir()) == []
    assert list(workspace["context_dir"].iterdir()) == []
