"""The agent profiles are {{TOKEN}} templates: they render with any install's values, and carry none."""
import importlib.util
import re

import pytest

from conftest import KIT


def _wp():
    spec = importlib.util.spec_from_file_location("profiles_workplace", KIT / "bin" / "workplace.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


WP = _wp()
TEMPLATES = sorted((KIT / "agents").glob("*.md.tmpl"))


@pytest.fixture(autouse=True)
def no_local_config(monkeypatch):
    monkeypatch.delenv("CAO_WORKPLACE_CONFIG", raising=False)
    monkeypatch.delenv("CAO_HOME_DIR", raising=False)


def config(tmp_path):
    """The shipped defaults plus a login, as if the kit sat in another folder."""
    cfg = WP.load(require=(), home=str(tmp_path / "home"), local={"github_login": "new-user",
                                                                   "repos": ["acme/widgets"]})
    root = str(tmp_path / "elsewhere" / "kit")
    cfg.update(workplace=root, kit=root, pb=root + "/pr-watch")
    return cfg


def test_the_two_profiles_ship():
    assert [p.name for p in TEMPLATES] == ["cao_conductor.md.tmpl", "pr_shepherd.md.tmpl"]


@pytest.mark.parametrize("src", TEMPLATES, ids=lambda p: p.name)
def test_a_profile_renders_with_a_new_install_and_keeps_no_placeholder(src, tmp_path):
    cfg = config(tmp_path)
    profile = src.name.removesuffix(".md.tmpl")
    text = WP.render_file(src, cfg, profile)
    assert str(tmp_path / "elsewhere" / "kit") in text
    assert "your-login" not in text and "your-org/your-repo" not in text
    assert "{{" not in text and f"provider: {cfg['profiles'][profile]['provider']}" in text
    if profile == "pr_shepherd":
        assert "new-user" in text and "acme/widgets" in text


@pytest.mark.parametrize("src", TEMPLATES, ids=lambda p: p.name)
def test_templates_use_only_known_tokens_and_no_literals(src):
    text = src.read_text()
    assert set(WP.TOKEN_RE.findall(text)) <= set(WP.TOKENS)
    assert not re.search(r"(?<![\w/-])your-login(?![\w/-])", text) and "/Users/" not in text
