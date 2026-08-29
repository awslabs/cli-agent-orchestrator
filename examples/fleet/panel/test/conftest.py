"""Point every test at a hermetic fixture registry (node-a/b/c), so the suite
runs without a real fleet.json and independently of any network."""
import os

import pytest

from app import config

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixture_fleet.json")


@pytest.fixture(autouse=True)
def _use_fixture_fleet(monkeypatch):
    monkeypatch.setattr(config, "FLEET_CONFIG", _FIXTURE)
    # The ConfigMap source off, and its snapshot empty, for every test that does
    # not ask for it. The snapshot is module-level and outlives a test function,
    # so without this a single test that populates it would silently redirect
    # every later test's `load_machines()` away from the fixture.
    monkeypatch.setattr(config, "FLEET_CONFIGMAP", None)
    monkeypatch.setitem(config._snapshot, "machines", None)
    monkeypatch.setitem(config._snapshot, "at", None)
    monkeypatch.setitem(config._snapshot, "error", None)
    monkeypatch.setitem(config._snapshot, "reads", 0)
