"""Tests for bin/workplace.py: the merged config, its fail-closed validation and the strict renderer."""
import importlib.util
import json
import shutil
import subprocess
import sys

import pytest

from conftest import KIT

DEFAULTS = KIT / "workplace.json"  # the example folder is its own workplace


def _module():
    spec = importlib.util.spec_from_file_location("wp_under_test", KIT / "bin" / "workplace.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


WP = _module()


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A kit folder (also the workplace root) with the shipped defaults and a copy of the helper, no local file."""
    monkeypatch.delenv("CAO_WORKPLACE_CONFIG", raising=False)
    monkeypatch.delenv("CAO_HOME_DIR", raising=False)
    r = tmp_path / "wp"
    (r / "bin").mkdir(parents=True)
    shutil.copyfile(DEFAULTS, r / "workplace.json")
    shutil.copyfile(KIT / "bin" / "workplace.py", r / "bin" / "workplace.py")
    return r


def local(root, **values):
    (root / "workplace.local.json").write_text(json.dumps(values))


def load(root, home="/home/u", **kw):
    return WP.load(root=str(root), home=home, **kw)


def reason(fn):
    with pytest.raises(WP.WorkplaceConfigError) as exc:
        fn()
    return exc.value.reason


def cli(root, *args, env_extra=None):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(root.parent / "home"), "PYTHONDONTWRITEBYTECODE": "1"}
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-I", str(root / "bin" / "workplace.py"), *args],
                          capture_output=True, text=True, env=env, timeout=30)


# ---- loading and merging

def test_defaults_load_without_a_login_only_when_not_required(root):
    cfg = load(root, require=())
    assert cfg["github_login"] is None
    assert cfg["repo"] == "your-org/your-repo"
    assert cfg["repo_url"] == "https://github.com/your-org/your-repo.git"
    assert cfg["kit"] == str(root) and cfg["pb"] == str(root / "pr-watch")
    assert cfg["cao_source_checkout"] == "/home/u/cli-agent-orchestrator"
    assert cfg["cao_home"] == "/home/u/.aws/cli-agent-orchestrator"
    assert reason(lambda: load(root)) == "workplace_login_unset"


def test_local_values_win_relay_merges_and_profiles_replace(root):
    local(root, github_login="new-user", relay={"enabled": True, "herdr_session": "relay-1"},
          profiles={"cao_conductor": {"provider": "codex", "model": "gpt-5"}})
    cfg = load(root)
    assert cfg["github_login"] == "new-user"
    assert cfg["relay"]["enabled"] is True and cfg["relay"]["herdr_session"] == "relay-1"
    assert cfg["relay"]["secrets_file"] == "/home/u/.config/herdr-remote/secrets.env"  # kept from defaults
    assert list(cfg["profiles"]) == ["cao_conductor"]  # replaced whole: the shepherd is dropped


def test_qwen_cli_is_an_allowed_provider(root):
    local(root, github_login="new-user", profiles={"pr_shepherd": {"provider": "qwen_cli", "model": "qwen"}})
    assert load(root)["profiles"]["pr_shepherd"]["provider"] == "qwen_cli"


def test_optional_deployment_keys_are_documented_as_reserved():
    text = (KIT / "bin" / "workplace.py").read_text()
    assert "dashboard_port, conductor_session and relay are reserved" in text


def test_cao_workplace_config_names_another_local_file(root, tmp_path, monkeypatch):
    other = tmp_path / "elsewhere.json"
    other.write_text('{"github_login": "from-env"}')
    monkeypatch.setenv("CAO_WORKPLACE_CONFIG", str(other))
    local(root, github_login="from-file")
    assert load(root)["github_login"] == "from-env"


def test_cao_home_dir_is_honoured(root, monkeypatch):
    monkeypatch.setenv("CAO_HOME_DIR", "/srv/cao")
    assert load(root, require=())["cao_home"] == "/srv/cao"


@pytest.mark.parametrize("values, why", [
    ({"github_token": "x"}, "workplace_unknown_key"),
    ({"relay": {"bot_token": "x"}}, "workplace_unknown_key"),
    ({"profiles": {"cao_conductor": {"provider": "codex", "api_key": "x"}}}, "workplace_unknown_key"),
    ({"github_login": "bad login"}, "workplace_bad_value"),
    ({"github_login": "-leading"}, "workplace_bad_value"),
    ({"repos": []}, "workplace_bad_value"),
    ({"repos": ["no-slash"]}, "workplace_bad_value"),
    ({"repos": ["a/b", "a/b"]}, "workplace_bad_value"),
    ({"repos": ["owner/.."]}, "workplace_bad_value"),
    ({"cao_api": "http://10.0.0.5:9889"}, "workplace_bad_value"),
    ({"cao_api": "http://[::1]:9889"}, "workplace_bad_value"),
    ({"cao_api": "https://localhost:9889"}, "workplace_bad_value"),
    ({"cao_api": "http://localhost:99999"}, "workplace_bad_value"),
    ({"dashboard_port": True}, "workplace_bad_value"),
    ({"dashboard_port": 0}, "workplace_bad_value"),
    ({"cao_source_checkout": "relative/path"}, "workplace_path_not_absolute"),
    ({"cao_source_checkout": "/a/{{KIT}}"}, "workplace_bad_value"),
    ({"profiles": {"cao_conductor": {"provider": "mock_cli"}}}, "workplace_bad_value"),
    ({"profiles": {"x": {"provider": "codex", "model": "has space"}}}, "workplace_bad_value"),
    ({"schema": 2}, "workplace_bad_schema"),
])
def test_bad_values_fail_closed(root, values, why):
    local(root, github_login="ok-user", **{k: v for k, v in values.items() if k != "github_login"})
    if "github_login" in values:
        local(root, **values)
    assert reason(lambda: load(root)) == why


@pytest.mark.parametrize("relay", [{"enabled": "yes"}, {"enabled": True, "tunnel_mode": "None"},
                                   {"enabled": True, "relay_port": 0}, {"enabled": True, "checkout": "rel/path"},
                                   {"enabled": True, "herdr_session": "bad session"}, "not-an-object"])
def test_a_bad_relay_value_turns_only_the_relay_off(root, relay, capsys):
    """Review P3-6: the relay is optional, so it never takes the login, repo or CAO API down with it."""
    local(root, github_login="ok-user", relay=relay)
    cfg = load(root)
    assert cfg["github_login"] == "ok-user" and cfg["repo"] == "your-org/your-repo"
    assert cfg["relay"]["enabled"] is False and cfg["relay"]["disabled_reason"].startswith("workplace_")
    assert any(w.startswith("relay disabled: ") for w in cfg["warnings"])
    assert "WARNING workplace: relay disabled" in capsys.readouterr().err


def test_a_good_relay_has_no_disabled_reason(root):
    local(root, github_login="ok-user", relay={"enabled": True, "herdr_session": "relay-1"})
    cfg = load(root)
    assert cfg["relay"]["enabled"] is True and cfg["relay"]["disabled_reason"] is None and cfg["warnings"] == []


def test_unreadable_or_missing_files_fail_closed(root):
    (root / "workplace.local.json").write_text("{not json")
    assert reason(lambda: load(root)) == "workplace_config_unreadable"
    (root / "workplace.local.json").write_text("[]")
    assert reason(lambda: load(root)) == "workplace_config_not_object"
    (root / "workplace.local.json").unlink()
    (root / "workplace.json").unlink()
    assert reason(lambda: load(root, require=())) == "workplace_defaults_missing"


# ---- rendering

def test_render_replaces_every_token_and_is_deterministic(root):
    local(root, github_login="new-user")
    cfg = load(root)
    text = "kit {{KIT}}/ pb {{PB}} by {{GITHUB_LOGIN}} on {{REPO}} at {{CAO_API}} src {{CAO_SOURCE}}\n"
    a = WP.render(text, WP.values(cfg))
    assert a == WP.render(text, WP.values(cfg))
    assert a == (f"kit {root}/ pb {root}/pr-watch by new-user on your-org/your-repo"
                 " at http://localhost:9889 src /home/u/cli-agent-orchestrator\n")


def test_render_profile_tokens_need_a_configured_profile(root):
    local(root, github_login="u")
    cfg = load(root)
    assert WP.render("{{PROVIDER}} {{MODEL}}", WP.values(cfg, "pr_shepherd")) == "claude_code sonnet"
    assert reason(lambda: WP.render("{{PROVIDER}}", WP.values(cfg))) == "render_token_unset"
    assert reason(lambda: WP.values(cfg, "cao_pr_reviewer")) == "workplace_profile_not_configured"


@pytest.mark.parametrize("text, why", [
    ("{{NOPE}}", "render_unknown_token"),
    ("{{ KIT }}", "render_token_left"),
    ("{{kit}}", "render_token_left"),
    ("{KIT}}", "render_token_left"),
    ("kit}} tail", "render_token_left"),
    ("{{GITHUB_LOGIN}}", "render_token_unset"),
])
def test_render_is_strict(root, text, why):
    cfg = load(root, require=())
    assert reason(lambda: WP.render(text, WP.values(cfg))) == why


def test_single_brace_placeholders_and_shell_vars_pass_through(root):
    cfg = load(root, require=())
    text = 'Fill {pr} and {head}; $CAO_TERMINAL_ID and ${HOME} and %{http_code} and {"prs": {}} stay\n'
    assert WP.render(text, WP.values(cfg)) == text


# ---- the CLI

def test_show_prints_values_and_fails_without_a_login(root):
    r = cli(root, "show")
    assert r.returncode == 1 and "workplace_login_unset" in r.stderr
    assert f"KIT={root}" in r.stdout.splitlines() and "LOGIN=" in r.stdout.splitlines()
    local(root, github_login="new-user")
    r = cli(root, "show")
    assert r.returncode == 0 and "LOGIN=new-user" in r.stdout.splitlines()
    assert f"LOCAL_FILE={root}/workplace.local.json" in r.stdout.splitlines() and "WARNING" not in r.stderr


def test_show_warns_when_the_override_is_set(root, tmp_path):
    """Review P3-7: CAO_WORKPLACE_CONFIG swaps the login and repo for every script; say so."""
    other = tmp_path / "other.json"
    other.write_text('{"github_login": "from-env"}')
    r = cli(root, "show", env_extra={"CAO_WORKPLACE_CONFIG": str(other)})
    assert r.returncode == 0 and "LOGIN=from-env" in r.stdout.splitlines()
    assert f"WARNING CAO_WORKPLACE_CONFIG is set: this install's values come from {other}" in r.stderr
    assert f"LOCAL_FILE={other}" in r.stdout.splitlines()


def test_get_reads_show_keys_and_dotted_config_keys(root):
    assert cli(root, "get", "api").stdout == "http://localhost:9889\n"
    assert cli(root, "get", "relay.enabled").stdout == "false\n"
    assert cli(root, "get", "relay.herdr_session").returncode == 1
    r = cli(root, "get", "LOGIN")
    assert r.returncode == 1 and "workplace_login_unset" in r.stderr
    assert cli(root, "get", "no.such").returncode == 1


def test_render_cli_writes_nothing_on_error(root, tmp_path):
    local(root, github_login="u")
    src, dst = tmp_path / "src.md", tmp_path / "out" / "dst.md"
    dst.parent.mkdir()
    src.write_text("a {{NOPE}}\n")
    r = cli(root, "render", str(src), str(dst))
    assert r.returncode == 1 and "render_unknown_token" in r.stderr
    assert list(dst.parent.iterdir()) == []
    src.write_text("model: {{MODEL}}\n")
    r = cli(root, "render", str(src), str(dst), "--profile", "cao_conductor")
    assert r.returncode == 0 and dst.read_text() == "model: opus\n"


def test_tracked_defaults_hold_no_login_and_load(root):
    doc = json.loads(DEFAULTS.read_text())
    assert doc["github_login"] is None
    assert set(doc) <= WP.TOP_KEYS
    load(root, require=())
