import pytest

from app import config


def test_missing_registry_raises_clear_error(monkeypatch):
    monkeypatch.setattr(config, "FLEET_CONFIG", "/no/such/fleet.json")
    with pytest.raises(RuntimeError, match="fleet registry not found"):
        config.load_machines()


def test_load_machines_has_three_with_ports():
    machines = config.load_machines()
    assert len(machines) == 3
    names = {m["name"] for m in machines}
    assert {"node-a", "node-b", "node-c"} <= names
    # every node resolves a port (falls back to the top-level default)
    assert all(isinstance(m["port"], int) for m in machines)


def test_base_url_format():
    node = next(m for m in config.load_machines() if m["name"] == "node-a")
    assert config.base_url(node) == "http://100.64.0.11:9889"
