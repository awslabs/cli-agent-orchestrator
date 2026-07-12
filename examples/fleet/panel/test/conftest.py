"""Point every test at a hermetic fixture registry (node-a/b/c), so the suite
runs without a real fleet.json and independently of any network."""
import os

import pytest

from app import config

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixture_fleet.json")


@pytest.fixture(autouse=True)
def _use_fixture_fleet(monkeypatch):
    monkeypatch.setattr(config, "FLEET_CONFIG", _FIXTURE)
