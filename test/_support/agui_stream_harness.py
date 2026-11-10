"""Drive the real ``/agui/v1/stream`` route with a fake event log and bus.

The fake bus yields a fixed list of live records and then ends, so every stream
closes deterministically (no test can hang). The fleet snapshot is pinned empty
unless a test overrides ``session_service``.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

import pytest


class FakeSubscriber:
    """Mirrors ``sse_bus._Subscriber``'s ``overflowed`` flag."""

    def __init__(self, overflowed: bool = False) -> None:
        self.overflowed = overflowed


class FakeBus:
    def __init__(self, live: Iterable[Dict[str, Any]], overflowed: bool = False) -> None:
        self._live = list(live)
        self._overflowed = overflowed
        self.registered: List[FakeSubscriber] = []

    def register(self, overflow_close: bool = False) -> FakeSubscriber:
        sub = FakeSubscriber()
        self.registered.append(sub)
        return sub

    def unregister(self, sub: Any) -> None:
        pass

    async def drain(self, sub: FakeSubscriber):
        for event in self._live:
            yield event
        # The real bus returns after the overflow sentinel with the flag set.
        sub.overflowed = self._overflowed


class FakeLog:
    def __init__(self, history: Iterable[Dict[str, Any]]) -> None:
        self._history = list(history)

    def history(self, since: Optional[str] = None, **_: Any) -> List[Dict[str, Any]]:
        if since is None:
            return list(self._history)
        return [e for e in self._history if str(e["timestamp"]) > since]

    def after_id(self, last_id: str) -> List[Dict[str, Any]]:
        ids = [e["id"] for e in self._history]
        if last_id not in ids:
            return list(self._history)
        return self._history[ids.index(last_id) + 1 :]


def install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    history: Iterable[Dict[str, Any]] = (),
    live: Iterable[Dict[str, Any]] = (),
    overflowed: bool = False,
    sessions: Optional[List[Dict[str, Any]]] = None,
    terminals: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> FakeBus:
    import cli_agent_orchestrator.api.main as main

    monkeypatch.setenv("CAO_AGUI_ENABLED", "true")
    monkeypatch.setattr(main, "is_auth_enabled", lambda: False)
    bus = FakeBus(live, overflowed=overflowed)
    log = FakeLog(history)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.event_log_service.get_event_log", lambda: log
    )
    monkeypatch.setattr("cli_agent_orchestrator.services.sse_bus.get_bus", lambda: bus)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.session_service.list_sessions",
        lambda: list(sessions or []),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.session_service.list_current_session_terminals",
        lambda name, **_: list((terminals or {}).get(name, [])),
    )
    return bus


class LegacyFramingClient:
    """A ``TestClient`` stand-in that pins ``GET /agui/v1/stream`` to ``framing=cao-legacy``.

    For test modules that pin the deprecated legacy wire (named SSE events,
    snake_case fields). The conformant stream has its own tests in
    ``test/api/test_agui_stream_conformance.py``.
    """

    def __init__(self, app: Any, **kwargs: Any) -> None:
        from fastapi.testclient import TestClient

        self._client = TestClient(app, **kwargs)

    @staticmethod
    def _pin(method: str, url: str, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        if method.upper() == "GET" and str(url).split("?")[0].endswith("/agui/v1/stream"):
            params = dict(kwargs.get("params") or {})
            params.setdefault("framing", "cao-legacy")
            kwargs = {**kwargs, "params": params}
        return kwargs

    def get(self, url: str, **kwargs: Any) -> Any:
        return self._client.get(url, **self._pin("GET", url, kwargs))

    def stream(self, method: str, url: str, **kwargs: Any) -> Any:
        return self._client.stream(method, url, **self._pin(method, url, kwargs))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)
