"""The internal recall route forwards the opt-in sort preference unchanged."""

from test.services.test_scoring import db_engine, hybrid_starvation_corpus, svc
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from cli_agent_orchestrator.api import main


def test_internal_recall_passes_relevance(svc, hybrid_starvation_corpus, monkeypatch):
    recall = AsyncMock(wraps=svc.recall)
    monkeypatch.setattr(svc, "recall", recall)
    monkeypatch.setattr(main, "_get_memory_service", lambda: svc)
    response = TestClient(main.app, base_url="http://localhost").post(
        "/internal/memory/recall",
        json={"query": "kafka streams", "scope": "global", "sort_by": "relevance", "limit": 1},
    )
    assert response.status_code == 200, response.text
    assert recall.await_args.kwargs["sort_by"] == "relevance"
    assert [m["key"] for m in response.json()["memories"]] == ["strong-old"]
