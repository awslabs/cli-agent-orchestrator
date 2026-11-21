"""Which CAO API the resource gate counts terminals on, and that it is always a loopback one.

Review P3-D1: the order is SUPER_AGENT_CAO_URL, then the resources file's cao_url, then workplace.json's
cao_api. Review P3-D2: the env value and cao_url get cao_api's own loopback rule.
"""
import json

import pytest

from conftest import KIT, base_env, run, write_json
from state_helpers import load

# Measurements with everything but the terminal count, so measure() must ask count_live_terminals.
NO_TERMINALS = {"pressure_level": 1, "free_memory_pct": 60, "swap_used_gib": 1.0, "disk_free_gib": 100.0,
                "load1": 0.5, "ncpu": 8}


@pytest.fixture
def gate(tmp_path, monkeypatch):
    """resource_gate in-process with a recording count_live_terminals and no workplace override."""
    rg = load("resource_gate")
    seen = []
    monkeypatch.setattr(rg, "count_live_terminals", lambda url, *a: seen.append(url) or (1, None))
    monkeypatch.setenv("SUPER_AGENT_FAKE_MEASUREMENTS", str(write_json(tmp_path / "fake.json", NO_TERMINALS)))
    monkeypatch.delenv("SUPER_AGENT_CAO_URL", raising=False)
    monkeypatch.delenv("CAO_WORKPLACE_CONFIG", raising=False)
    cfg = json.loads((KIT / "resources.json").read_text())
    return rg, cfg, seen


def local(tmp_path, monkeypatch, text):
    p = tmp_path / "workplace.local.json"
    p.write_text(text)
    monkeypatch.setenv("CAO_WORKPLACE_CONFIG", str(p))


def test_env_beats_resources_beats_workplace(gate, tmp_path, monkeypatch):
    rg, cfg, seen = gate
    local(tmp_path, monkeypatch, '{"cao_api": "http://127.0.0.1:4003"}')
    monkeypatch.setenv("SUPER_AGENT_CAO_URL", "http://127.0.0.1:4001")
    rg.measure({**cfg, "cao_url": "http://127.0.0.1:4002"})
    monkeypatch.delenv("SUPER_AGENT_CAO_URL")
    rg.measure({**cfg, "cao_url": "http://127.0.0.1:4002"})
    rg.measure({k: v for k, v in cfg.items() if k != "cao_url"})
    assert seen == ["http://127.0.0.1:4001", "http://127.0.0.1:4002", "http://127.0.0.1:4003"]


def test_the_shipped_resources_fall_back_to_the_workplace_cao_api(gate, tmp_path, monkeypatch):
    rg, cfg, seen = gate
    assert "cao_url" not in cfg  # the portable bootstrap moved it to workplace.json
    local(tmp_path, monkeypatch, '{"cao_api": "http://localhost:4004"}')
    rg.measure(cfg)
    assert seen == ["http://localhost:4004"]


def test_an_unreadable_workplace_config_gives_no_url(gate, tmp_path, monkeypatch):
    rg, cfg, seen = gate
    local(tmp_path, monkeypatch, "{broken")
    rg.measure(cfg)
    assert seen == [None]


@pytest.mark.parametrize("url", ["http://10.0.0.5:9889", "http://example.com:9889", "http://[::1]:9889",
                                 "https://localhost:9889", "http://localhost:9889/", "http://127.0.0.1:0",
                                 "http://127.0.0.1:99999", "http://localhost", "ftp://127.0.0.1:9889"])
def test_count_refuses_a_non_loopback_url_without_any_request(url, monkeypatch):
    rg = load("resource_gate")
    monkeypatch.setattr(rg.urllib.request, "build_opener", lambda *a: pytest.fail("a request was attempted"))
    assert rg.count_live_terminals(url) == (None, "cao_api:not_loopback")


@pytest.mark.parametrize("url", ["http://127.0.0.1:9889", "http://localhost:9889"])
def test_loopback_urls_pass_the_rule(url):
    assert load("resource_gate").is_loopback_api(url)


def test_a_non_loopback_env_url_throttles_end_to_end(tmp_path):
    fake = write_json(tmp_path / "fake.json", NO_TERMINALS)
    env = base_env(tmp_path, SUPER_AGENT_FAKE_MEASUREMENTS=fake, SUPER_AGENT_CAO_URL="http://10.1.2.3:9889")
    r = run("resource_gate.py", "--resources", KIT / "resources.json", env=env)
    assert r.returncode == 3 and r.stdout.startswith("THROTTLE") and "cao_api:not_loopback" in r.stdout


def test_a_non_loopback_resources_cao_url_is_a_config_error(tmp_path):
    cfg = json.loads((KIT / "resources.linux.json").read_text())
    cfg["cao_url"] = "http://192.168.1.9:9889"
    path = write_json(tmp_path / "resources.json", cfg)
    r = run("resource_gate.py", "--resources", path, env=base_env(tmp_path))
    assert r.returncode == 1 and r.stdout.startswith("STOP bad_config:cao_url:not_loopback")
