"""Reserved names cannot enter installed stores through any import surface."""

from unittest.mock import Mock

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from cli_agent_orchestrator.services import install_service, profile_store
from cli_agent_orchestrator.utils import agent_profiles, env, skill_injection

RESERVED = "ACDC-log_triage-3f9a"
SURFACES = ["name", "content", "cli-file", "cli-url", "api-name", "api-url"]
PROVIDERS = ["claude_code", "kiro_cli", "copilot_cli", "opencode_cli"]


def document(name):
    return f"---\nname: {name}\ndescription: Test\n---\nTask.\n"


@pytest.fixture
def paths(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services import settings_service

    paths = {key: tmp_path / key for key in ("store", "context", "kiro", "copilot", "opencode")}
    for path in paths.values():
        path.mkdir()
    monkeypatch.setattr(profile_store, "LOCAL_AGENT_STORE_DIR", paths["store"])
    monkeypatch.setattr(agent_profiles, "LOCAL_AGENT_STORE_DIR", paths["store"])
    for key, attr in [
        ("context", "AGENT_CONTEXT_DIR"),
        ("kiro", "KIRO_AGENTS_DIR"),
        ("copilot", "COPILOT_AGENTS_DIR"),
        ("opencode", "OPENCODE_AGENTS_DIR"),
    ]:
        monkeypatch.setattr(install_service, attr, paths[key])
    monkeypatch.setattr(settings_service, "get_agent_dirs", lambda: {})
    monkeypatch.setattr(settings_service, "get_extra_agent_dirs", lambda: [])
    monkeypatch.setattr(settings_service, "get_disabled_agent_dirs", lambda: [])
    monkeypatch.setattr(skill_injection, "build_skill_catalog", lambda: "")
    seam = Mock(side_effect=AssertionError("environment file seam reached"))
    monkeypatch.setattr(env, "load_env_vars", seam)
    monkeypatch.setattr(install_service, "load_env_vars", Mock(return_value={}))
    monkeypatch.setattr(install_service, "set_env_var", seam)
    resolver = Mock(side_effect=lambda content: content)
    monkeypatch.setattr(agent_profiles, "resolve_env_vars", resolver)
    yield paths, seam, resolver
    seam.assert_not_called()


def invoke(surface, name, provider, tmp_path, monkeypatch, content=None):
    from cli_agent_orchestrator.api.main import app
    from cli_agent_orchestrator.cli.commands.install import install

    content = document(name) if content is None else content
    url = f"https://raw.githubusercontent.com/example/repo/main/{name}.md"
    download = Mock(return_value=(name, content))
    monkeypatch.setattr(install_service, "_download_agent", download)
    if surface in {"cli-file", "cli-url"}:
        source = url
        if surface == "cli-file":
            source = tmp_path / f"{name}.md"
            source.write_text(content)
        result = CliRunner().invoke(install, [str(source), "--provider", provider])
        assert result.exit_code == 0, result.output
        success, message = "installed successfully" in result.output, result.output
    elif surface in {"api-name", "api-url"}:
        source = url if surface == "api-url" else name
        result = TestClient(app, base_url="http://localhost").post(
            "/agents/profiles/install", json={"source": source, "provider": provider}
        )
        assert result.status_code in {200, 400}, result.text
        success, message = result.status_code == 200, result.text
    else:
        kwargs = {"profile_content": content} if surface == "content" else {}
        result = install_service.install_agent(name, provider=provider, **kwargs)
        success, message = result.success, result.message
    if surface in {"cli-url", "api-url"}:
        download.assert_called_once_with(url)
    else:
        download.assert_not_called()
    return success, message


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("reserved", [True, False])
def test_install_namespace(surface, provider, reserved, paths, tmp_path, monkeypatch):
    stores, seam, resolver = paths
    name = RESERVED if reserved else "ordinary"
    # Bare names must be refused even when an installed collision already exists.
    original = document(name)
    if surface in {"name", "api-name"}:
        (stores["store"] / f"{name}.md").write_text(original)
    before = {p.name: p.read_bytes() for p in stores["store"].iterdir()}
    guard = Mock(wraps=install_service._guard_installed_copy_ownership)
    monkeypatch.setattr(install_service, "_guard_installed_copy_ownership", guard)
    success, message = invoke(surface, name, provider, tmp_path, monkeypatch)
    assert success is not reserved, message
    seam.assert_not_called()
    if reserved:
        assert f"Reserved ephemeral profile name: {name}" in message
        guard.assert_not_called()
        resolver.assert_not_called()
        install_service.load_env_vars.assert_not_called()
        assert {p.name: p.read_bytes() for p in stores["store"].iterdir()} == before
        for key in ("context", "kiro", "copilot", "opencode"):
            assert list(stores[key].iterdir()) == []
    else:
        guard.assert_called_once()
        assert (stores["store"] / f"{name}.md").is_file()
        assert (stores["context"] / f"{name}.md").is_file()
        if provider != "claude_code":
            key = {"kiro_cli": "kiro", "copilot_cli": "copilot", "opencode_cli": "opencode"}[
                provider
            ]
            assert any(stores[key].iterdir())


@pytest.mark.parametrize("surface", ["content", "cli-file", "cli-url", "api-url"])
def test_reserved_frontmatter_cannot_write_provider_artifacts(
    surface, paths, tmp_path, monkeypatch
):
    stores, _, _ = paths
    success, message = invoke(
        surface, "ordinary", "kiro_cli", tmp_path, monkeypatch, document(RESERVED)
    )
    assert not success
    assert f"Reserved ephemeral profile name: {RESERVED}" in message
    install_service.load_env_vars.assert_not_called()
    assert all(list(path.iterdir()) == [] for path in stores.values())


@pytest.mark.parametrize("writer", ["write", "overwrite", "replace"])
@pytest.mark.parametrize("reserved", [True, False])
def test_profile_store_writers_refuse_reserved_names(writer, reserved, paths):
    stores, _, _ = paths
    name = RESERVED if reserved else "ordinary"
    target = stores["store"] / f"{name}.md"
    if writer in {"overwrite", "replace"}:
        target.write_text("previous bytes")
    call = lambda: profile_store.write_profile(
        name, document(name), overwrite=writer == "overwrite"
    )
    if writer == "replace":
        call = lambda: profile_store.replace_profile(name, document(name))
    if reserved:
        with pytest.raises(FileNotFoundError, match=f"Reserved ephemeral profile name: {name}"):
            call()
        if writer == "write":
            assert list(stores["store"].iterdir()) == []
        else:
            assert target.read_text() == "previous bytes"
            assert list(stores["store"].iterdir()) == [target]
    else:
        assert call() == target
        assert target.read_text() == document(name)


@pytest.mark.parametrize("reserved", [True, False])
def test_copilot_refresh_refuses_reserved_profiles(reserved, paths):
    from cli_agent_orchestrator.utils.agent_profiles import parse_agent_profile_text

    stores, _, _ = paths
    name = RESERVED if reserved else "ordinary"
    target = stores["copilot"] / f"{name}.agent.md"
    target.write_text(document(name))
    profile = parse_agent_profile_text(document(name).replace("Task.", "Updated."), name)
    if reserved:
        with pytest.raises(FileNotFoundError, match=f"Reserved ephemeral profile name: {name}"):
            skill_injection.refresh_agent_md_prompt(target, profile)
        assert target.read_text() == document(name)
        assert list(stores["copilot"].iterdir()) == [target]
    else:
        assert skill_injection.refresh_agent_md_prompt(target, profile)
        assert "Updated." in target.read_text()


@pytest.mark.parametrize("source_kind", ["name", "content", "url", "frontmatter"])
@pytest.mark.parametrize("has_env", [False, True])
def test_reserved_install_never_resolves_or_persists_supplied_env(
    source_kind, has_env, paths, monkeypatch
):
    stores, seam, resolver = paths
    download = Mock(return_value=(RESERVED, document(RESERVED)))
    monkeypatch.setattr(install_service, "_download_agent", download)
    source = (
        f"https://raw.githubusercontent.com/example/repo/main/{RESERVED}.md"
        if source_kind == "url"
        else RESERVED
    )
    kwargs = (
        {"profile_content": document(RESERVED)} if source_kind in {"content", "frontmatter"} else {}
    )
    if source_kind == "frontmatter":
        source = "ordinary"
    guard = Mock(wraps=install_service._guard_installed_copy_ownership)
    monkeypatch.setattr(install_service, "_guard_installed_copy_ownership", guard)
    result = install_service.install_agent(
        source, provider="kiro_cli", env_vars={"ALIAS": "ordinary"} if has_env else None, **kwargs
    )
    assert not result.success
    assert result.message == f"Reserved ephemeral profile name: {RESERVED}"
    guard.assert_not_called()
    seam.assert_not_called()
    resolver.assert_not_called()
    install_service.load_env_vars.assert_not_called()
    assert all(list(path.iterdir()) == [] for path in stores.values())


def test_copilot_refresh_refuses_reserved_filename_with_ordinary_profile(paths):
    stores, _, _ = paths
    target = stores["copilot"] / f"{RESERVED}.agent.md"
    target.write_text(document("ordinary"))
    profile = agent_profiles.parse_agent_profile_text(document("ordinary"), "ordinary")
    with pytest.raises(FileNotFoundError, match=f"Reserved ephemeral profile name: {RESERVED}"):
        skill_injection.refresh_agent_md_prompt(target, profile)
    assert target.read_text() == document("ordinary")
    assert list(stores["copilot"].iterdir()) == [target]


@pytest.mark.parametrize("has_env", [False, True])
def test_variable_built_reserved_name_is_refused_after_resolution(has_env, paths, monkeypatch):
    stores, seam, resolver = paths
    content = document("${ALIAS}")
    loader = Mock(return_value={"ALIAS": "ordinary" if has_env else RESERVED})
    monkeypatch.setattr(install_service, "load_env_vars", loader)
    guard = Mock(wraps=install_service._guard_installed_copy_ownership)
    monkeypatch.setattr(install_service, "_guard_installed_copy_ownership", guard)
    result = install_service.install_agent(
        "ordinary",
        provider="kiro_cli",
        profile_content=content,
        env_vars={"ALIAS": RESERVED} if has_env else None,
    )
    assert not result.success
    assert result.message == f"Reserved ephemeral profile name: {RESERVED}"
    guard.assert_not_called()
    seam.assert_not_called()
    loader.assert_called_once()
    resolver.assert_not_called()
    assert all(list(path.iterdir()) == [] for path in stores.values())


def flow_document(name, shape, provider="kiro_cli"):
    field = "env: {TOKEN: ${TOKEN}}" if shape == "mapping" else "args: [--token, ${TOKEN}]"
    return (
        f"---\nname: {name}\ndescription: Flow placeholders\nprovider: {provider}\n"
        f"mcpServers:\n  helper:\n    command: fake-server\n    {field}\n---\nTask.\n"
    )


@pytest.mark.parametrize("surface", ["cli-file", "api-url"])
@pytest.mark.parametrize("shape", ["mapping", "sequence"])
@pytest.mark.parametrize("reserved", [False, True])
def test_flow_placeholders_resolve_before_parsed_name_refusal(
    paths, tmp_path, monkeypatch, surface, shape, reserved
):
    from cli_agent_orchestrator.api.main import app
    from cli_agent_orchestrator.cli.commands.install import install

    stores, seam, _ = paths
    content = flow_document(RESERVED if reserved else "ordinary", shape)
    loader = Mock(return_value={})
    persist = Mock()
    guard = Mock(wraps=install_service._guard_installed_copy_ownership)
    monkeypatch.setattr(install_service, "load_env_vars", loader)
    monkeypatch.setattr(install_service, "set_env_var", persist)
    monkeypatch.setattr(install_service, "_guard_installed_copy_ownership", guard)
    if surface == "cli-file":
        source = tmp_path / "ordinary.md"
        source.write_text(content)
        result = CliRunner().invoke(
            install, [str(source), "--provider", "kiro_cli", "--env", "TOKEN=abc"]
        )
        success, message = "installed successfully" in result.output, result.output
    else:
        monkeypatch.setattr(install_service, "_download_agent", lambda url: ("ordinary", content))
        result = TestClient(app, base_url="http://localhost").post(
            "/agents/profiles/install",
            json={
                "source": "https://raw.githubusercontent.com/example/repo/main/ordinary.md",
                "provider": "kiro_cli",
                "env_vars": {"TOKEN": "abc"},
            },
        )
        success, message = result.status_code == 200, result.text
    assert success is not reserved, message
    loader.assert_called_once()
    seam.assert_not_called()
    if reserved:
        assert f"Reserved ephemeral profile name: {RESERVED}" in message
        guard.assert_not_called()
        persist.assert_not_called()
        assert all(list(path.iterdir()) == [] for path in stores.values())
    else:
        guard.assert_called_once()
        persist.assert_called_once_with("TOKEN", "abc")
        assert (stores["store"] / "ordinary.md").is_file()
        assert (stores["context"] / "ordinary.md").is_file()
        assert (stores["kiro"] / "ordinary.json").is_file()


def test_raw_name_precheck_does_not_swallow_non_yaml_errors(paths, monkeypatch):
    stores, seam, resolver = paths
    monkeypatch.setattr(
        install_service.frontmatter,
        "loads",
        Mock(side_effect=RuntimeError("unexpected parser error")),
    )
    result = install_service.install_agent(
        "ordinary", provider="kiro_cli", profile_content=document("ordinary")
    )
    assert not result.success
    assert "unexpected parser error" in result.message
    resolver.assert_not_called()
    seam.assert_not_called()
    assert all(list(path.iterdir()) == [] for path in stores.values())
