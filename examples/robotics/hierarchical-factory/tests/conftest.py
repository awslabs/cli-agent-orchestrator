from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def no_robot_model_download(tmp_path_factory, monkeypatch):
    """Use the simple bipeds unless a test points at a model, so tests need no download."""
    monkeypatch.setenv("FACTORY_ROBOT_MODEL_DIR", str(tmp_path_factory.mktemp("no-robot-model")))
