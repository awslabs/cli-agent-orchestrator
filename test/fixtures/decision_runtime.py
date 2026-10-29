"""Observable default-off runtime for ASGI tests that omit lifespan startup."""

from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import pytest


@dataclass
class RecordAccessGuard:
    accesses: int = 0

    def __call__(self) -> NoReturn:
        self.accesses += 1
        raise RuntimeError("default-off test runtime accessed decision records")

    def check(self) -> None:
        assert self.accesses == 0, "default-off test runtime accessed decision records"


def isolated_runtime(root: Path):
    from cli_agent_orchestrator.decisions.engine import DecisionEngine
    from cli_agent_orchestrator.decisions.registry import DeciderRegistry
    from cli_agent_orchestrator.decisions.settings import DecisionSettings
    from cli_agent_orchestrator.decisions.shadow import ShadowRunner
    from cli_agent_orchestrator.decisions.store import DecisionStore

    guard = RecordAccessGuard()
    store = DecisionStore(guard, root / "decision-hash.key")
    runtime = DecisionEngine(
        DeciderRegistry({}), store, ShadowRunner(store), settings_loader=DecisionSettings
    )
    return runtime, guard


@pytest.fixture(autouse=True)
def default_decision_runtime(tmp_path, monkeypatch):
    from cli_agent_orchestrator.api.main import app

    runtime, guard = isolated_runtime(tmp_path)
    monkeypatch.setattr(app.state, "decision_engine", runtime, raising=False)
    yield runtime
    guard.check()
