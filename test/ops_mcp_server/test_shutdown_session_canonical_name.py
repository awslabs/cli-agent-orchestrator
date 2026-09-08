"""shutdown_session against the REAL delete route and deletion service.

The bare-name contract cannot be proven with a mocked DELETE. ``DELETE
/sessions/{name}`` is idempotent: ``session_service.delete_session()`` puts an
absent name straight into ``deleted``, so the route answers HTTP 200 for a
session that never existed. A test that mocks a 404 DELETE therefore asserts a
response the API never produces, and a bare-name delete that silently no-ops
while ``cao-<name>`` stays live would pass it.

So these tests drive the ops tool's real ``requests`` call into an in-process
``TestClient`` over the real FastAPI app (the seam ``test/mcp_server/conftest.py``
established for the in-session MCP tools), with the REAL ``delete_session``
reconciliation running underneath it against the in-memory tmux backend and the
per-test SQLite registry that ``test/services/test_session_teardown_atomic.py``
already models the teardown side effects with. What is asserted is the thing the
coordinator cares about: the session is actually gone from the backend and the
registry, and the tool issued exactly one destructive request.

The routed responses are httpx responses rather than ``requests`` ones. That is
faithful for this surface: the ops helpers only read ``status_code``/``json()``/
``text``, and both libraries raise a ``ValueError`` subclass from ``json()`` on a
non-JSON body. No in-process transport can raise ``requests.RequestException``,
which is what the mocked unit tests in ``test_server.py`` still cover.
"""

from __future__ import annotations

from test.api.conftest import TestClientWithHost
from test.services.test_session_teardown_atomic import FakeTmuxBackend
from typing import Dict, List, Tuple

import pytest
import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.backends.registry import set_backend
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.ops_mcp_server import server as ops_server
from cli_agent_orchestrator.plugins import PluginRegistry
from cli_agent_orchestrator.services import terminal_service


class ListableFakeTmuxBackend(FakeTmuxBackend):
    """``FakeTmuxBackend`` plus the listing ``GET /sessions/{name}`` needs.

    ``session_service.get_session`` resolves a session out of
    ``list_sessions()``, which the teardown-focused fake has no reason to
    implement. Everything the delete path exercises is inherited unchanged.
    """

    def list_sessions(self) -> List[Dict[str, str]]:
        return [{"id": name, "name": name, "status": "detached"} for name in sorted(self._sessions)]


@pytest.fixture
def real_db(tmp_path, monkeypatch):
    """Point ``clients.database`` at a fresh per-test SQLite registry."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'cao.db'}",
        connect_args={"check_same_thread": False},
    )
    database.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(
        database,
        "SessionLocal",
        sessionmaker(autocommit=False, autoflush=False, bind=engine),
    )
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(terminal_service, "TERMINAL_LOG_DIR", log_dir)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def backend(monkeypatch):
    """Install the in-memory tmux backend for the duration of the test."""
    fake = ListableFakeTmuxBackend()
    set_backend(fake)

    # Teardown touches the FIFO reader, status buffers and provider registration
    # of each terminal; none of those exist in-process here.
    monkeypatch.setattr(terminal_service.fifo_manager, "stop_reader", lambda tid: None)
    monkeypatch.setattr(terminal_service.status_monitor, "clear_terminal", lambda tid: None)
    monkeypatch.setattr(terminal_service.provider_manager, "cleanup_provider", lambda tid: None)

    # GET /sessions/{name} asks the status monitor for each terminal's live
    # status, which would shell out to tmux.
    from cli_agent_orchestrator.services import status_monitor as status_monitor_module

    monkeypatch.setattr(
        status_monitor_module.status_monitor,
        "get_status",
        lambda tid: TerminalStatus.IDLE,
    )
    try:
        yield fake
    finally:
        set_backend(None)  # type: ignore[arg-type]


@pytest.fixture
def routed_requests(monkeypatch):
    """Route ``ops_mcp_server.server.requests.request`` into a TestClient.

    Returns the list of ``(method, path)`` pairs that reached the app, so a test
    can assert how many destructive requests were issued, not merely what came
    back.
    """
    app.state.plugin_registry = PluginRegistry()
    client = TestClientWithHost(app)
    calls: List[Tuple[str, str]] = []

    def _dispatch(method, url, params=None, json=None):
        # The helpers build f"{API_BASE_URL}{path}"; TestClient wants the path.
        path = "/" + str(url).split("://", 1)[1].split("/", 1)[1]
        calls.append((method.lower(), path))
        return client.request(method.upper(), path, params=params, json=json)

    monkeypatch.setattr(ops_server.requests, "request", _dispatch)
    return calls


def _seed(backend, session_name: str, terminals: List[Tuple[str, str]]) -> None:
    """Create backend windows plus the matching registry rows."""
    backend.add_session(session_name, {window for _, window in terminals})
    for terminal_id, window_name in terminals:
        database.create_terminal(
            terminal_id=terminal_id,
            tmux_session=session_name,
            tmux_window=window_name,
            provider="claude_code",
            agent_profile="developer",
        )


def _deletes(calls: List[Tuple[str, str]]) -> List[str]:
    return [path for method, path in calls if method == "delete"]


@pytest.mark.asyncio
class TestShutdownSessionAgainstRealRoute:
    """The bare/canonical name contract, proven against the real endpoint."""

    async def test_bare_name_deletes_the_prefixed_session(
        self, real_db, backend, routed_requests
    ) -> None:
        """``shutdown_session("review-alias")`` must kill ``cao-review-alias``.

        This is the case a 404-triggered retry cannot reach: the bare-name DELETE
        succeeds with ``deleted: ["review-alias"]`` while the real session lives
        on, so the coordinator is told cleanup worked when nothing was killed.
        """
        _seed(backend, "cao-review-alias", [("t1", "w1")])

        result = await ops_server.shutdown_session("review-alias")

        # The session is really gone from BOTH stores ...
        assert backend.session_exists("cao-review-alias") is False
        assert database.list_terminals_by_session("cao-review-alias") == []
        # ... and the caller is told so under the canonical name.
        assert result["success"] is True
        assert result["deleted"] == ["cao-review-alias"]
        assert result["errors"] == []
        # Exactly one destructive request, against the resolved name. The bare
        # name is never sent to DELETE: that request would "succeed" as a no-op.
        assert _deletes(routed_requests) == ["/sessions/cao-review-alias"]

    async def test_already_canonical_name_is_unchanged(
        self, real_db, backend, routed_requests
    ) -> None:
        """A caller that already passes ``cao-<name>`` sees the prior behaviour."""
        _seed(backend, "cao-canonical", [("t1", "w1")])

        result = await ops_server.shutdown_session("cao-canonical")

        assert backend.session_exists("cao-canonical") is False
        assert database.list_terminals_by_session("cao-canonical") == []
        assert result["success"] is True
        assert result["deleted"] == ["cao-canonical"]
        assert result["errors"] == []
        assert _deletes(routed_requests) == ["/sessions/cao-canonical"]

    async def test_missing_session_keeps_idempotent_success(
        self, real_db, backend, routed_requests
    ) -> None:
        """Nothing under either name: still the endpoint's idempotent success.

        Confirmed absence must not turn "already gone" into a new error, and it
        must not fan out into a second delete. The single delete goes to the
        CANONICAL name -- the cleanup identity the naming contract gives this
        alias -- which for a name that never existed is an equally idempotent
        no-op.
        """
        result = await ops_server.shutdown_session("ghost")

        assert result["success"] is True
        assert result["deleted"] == ["cao-ghost"]
        assert result["errors"] == []
        assert _deletes(routed_requests) == ["/sessions/cao-ghost"]

    async def test_get_session_info_resolves_bare_name(
        self, real_db, backend, routed_requests
    ) -> None:
        """The read path's prefix retry works against the real 404 too."""
        _seed(backend, "cao-readable", [("t1", "w1")])

        result = await ops_server.get_session_info("readable")

        assert result["session"]["id"] == "cao-readable"
        assert [t["id"] for t in result["terminals"]] == ["t1"]
        assert _deletes(routed_requests) == []

    async def test_unresolvable_lookup_deletes_nothing(
        self, real_db, backend, routed_requests, monkeypatch
    ) -> None:
        """A 500 from the canonical GET must not be read as "absent".

        ``GET /sessions/{name}`` really can fail with 500 while the session is
        alive: reading a terminal's live status is part of the handler, and
        ``api/main.py`` maps any non-ValueError to 500. Treating that as
        confirmed absence used to DELETE the bare alias -- a 200 no-op -- and
        report cleanup success while the canonical session and its registry row
        stayed exactly where they were.
        """
        _seed(backend, "cao-flaky", [("t1", "w1")])

        from cli_agent_orchestrator.services import status_monitor as status_monitor_module

        def _unreadable_status(terminal_id: str) -> TerminalStatus:
            raise RuntimeError("status socket unavailable")

        monkeypatch.setattr(status_monitor_module.status_monitor, "get_status", _unreadable_status)

        result = await ops_server.shutdown_session("flaky")

        # Nothing was mutated at all ...
        assert _deletes(routed_requests) == []
        assert backend.session_exists("cao-flaky") is True
        assert [t["id"] for t in database.list_terminals_by_session("cao-flaky")] == ["t1"]
        # ... and the coordinator is told why, under the name that failed.
        assert result["success"] is False
        assert "cao-flaky" in result["message"]
        assert "status socket unavailable" in result["message"]

    async def test_lookup_transport_failure_deletes_nothing(
        self, real_db, backend, routed_requests, monkeypatch
    ) -> None:
        """An unreachable API is unresolved too, not evidence of absence.

        This is the one case the in-process transport cannot produce on its own
        (see the module docstring), so the routed dispatcher is wrapped to raise
        on the lookups only -- a DELETE, if the tool wrongly issued one, would
        still reach the real app and be recorded.
        """
        _seed(backend, "cao-unreachable", [("t1", "w1")])

        routed = ops_server.requests.request

        def _failing_get(method, url, params=None, json=None):
            if method.lower() == "get":
                raise requests.ConnectionError("connection refused")
            return routed(method, url, params=params, json=json)

        monkeypatch.setattr(ops_server.requests, "request", _failing_get)

        result = await ops_server.shutdown_session("unreachable")

        assert _deletes(routed_requests) == []
        assert backend.session_exists("cao-unreachable") is True
        assert [t["id"] for t in database.list_terminals_by_session("cao-unreachable")] == ["t1"]
        assert result["success"] is False
        assert "connection refused" in result["message"]

    async def test_deferred_cleanup_retry_with_bare_name_finishes_the_job(
        self, real_db, backend, routed_requests, monkeypatch
    ) -> None:
        """The supported cleanup retry, end to end, driven with the BARE name.

        A deferred provider cleanup (#596) is a partially-complete teardown: the
        backend session is killed, but the registry row is KEPT as the retry
        handle and the session comes back in ``errors`` as a 409. The retry is
        the case live-backend presence cannot resolve -- ``get_session``
        requires the backend session, so by then BOTH GETs 404 while the row
        that still needs cleaning lives under the canonical name. Targeting the
        typed alias would "succeed" without retrying anything.
        """
        _seed(backend, "cao-deferred", [("t1", "w1")])

        deferred = {"pending": True}

        def _cleanup_provider(terminal_id: str):
            # False is exactly what defers the teardown in
            # terminal_service.dismantle_terminal_runtime.
            return False if deferred["pending"] else None

        monkeypatch.setattr(
            terminal_service.provider_manager, "cleanup_provider", _cleanup_provider
        )

        first = await ops_server.shutdown_session("deferred")

        assert first["success"] is False
        assert "cleanup deferred" in first["message"]
        assert _deletes(routed_requests) == ["/sessions/cao-deferred"]
        # Backend gone, row kept: the state the retry has to reconcile.
        assert backend.session_exists("cao-deferred") is False
        assert [t["id"] for t in database.list_terminals_by_session("cao-deferred")] == ["t1"]

        deferred["pending"] = False
        routed_requests.clear()

        second = await ops_server.shutdown_session("deferred")

        assert second["success"] is True
        assert second["deleted"] == ["cao-deferred"]
        assert second["errors"] == []
        assert database.list_terminals_by_session("cao-deferred") == []
        assert _deletes(routed_requests) == ["/sessions/cao-deferred"]

    async def test_bare_name_never_targets_a_coexisting_native_session(
        self, real_db, backend, routed_requests
    ) -> None:
        """A native tmux ``review`` and a CAO ``cao-review`` can coexist.

        ``session_service.get_session`` reads the backend directly, WITHOUT the
        SESSION_PREFIX filter ``list_sessions`` applies, and the shipped tmux
        backend lists native sessions too -- so both names answer GET. Probing
        the literal name first and taking any success deleted the unrelated
        NATIVE session, reported ``success: true``, and left ``cao-review`` and
        its row alive. The canonical name is the only cleanup identity.
        """
        backend.add_session("review", {"native-w"})  # not created through CAO: no rows
        _seed(backend, "cao-review", [("t1", "w1")])

        result = await ops_server.shutdown_session("review")

        # The CAO session is gone from both stores ...
        assert backend.session_exists("cao-review") is False
        assert database.list_terminals_by_session("cao-review") == []
        # ... and the native session was never touched.
        assert backend.session_exists("review") is True
        assert backend.windows("review") == {"native-w"}
        assert result["success"] is True
        assert result["deleted"] == ["cao-review"]
        assert result["errors"] == []
        assert _deletes(routed_requests) == ["/sessions/cao-review"]

    async def test_get_session_info_reads_the_cao_session_not_the_native_one(
        self, real_db, backend, routed_requests
    ) -> None:
        """The read path shares the same identity rule.

        With both present, the literal-first lookup returned the native session
        -- which has no CAO terminals at all -- as if it were the CAO one.
        """
        backend.add_session("review", {"native-w"})
        _seed(backend, "cao-review", [("t1", "w1")])

        result = await ops_server.get_session_info("review")

        assert result["session"]["id"] == "cao-review"
        assert [t["id"] for t in result["terminals"]] == ["t1"]
        assert _deletes(routed_requests) == []

    async def test_deferred_retry_with_only_the_native_session_live(
        self, real_db, backend, routed_requests, monkeypatch
    ) -> None:
        """The retained-row retry, with a native session under the bare name.

        This is where the two defects meet: after the deferred first delete the
        CAO backend session is gone, so the canonical GET 404s, while the native
        ``review`` still answers one. Treating that answer as the target killed
        the native session and left the canonical retry row behind.
        """
        backend.add_session("review", {"native-w"})
        _seed(backend, "cao-review", [("t1", "w1")])

        deferred = {"pending": True}
        monkeypatch.setattr(
            terminal_service.provider_manager,
            "cleanup_provider",
            lambda terminal_id: False if deferred["pending"] else None,
        )

        first = await ops_server.shutdown_session("review")

        assert first["success"] is False
        assert "cleanup deferred" in first["message"]
        assert _deletes(routed_requests) == ["/sessions/cao-review"]
        # Only the native session is live now; the CAO row is the retry handle.
        assert backend.session_exists("cao-review") is False
        assert backend.session_exists("review") is True
        assert [t["id"] for t in database.list_terminals_by_session("cao-review")] == ["t1"]

        deferred["pending"] = False
        routed_requests.clear()

        second = await ops_server.shutdown_session("review")

        assert second["success"] is True
        assert second["deleted"] == ["cao-review"]
        assert second["errors"] == []
        assert database.list_terminals_by_session("cao-review") == []
        assert _deletes(routed_requests) == ["/sessions/cao-review"]
        # The native session survived both attempts.
        assert backend.session_exists("review") is True
        assert backend.windows("review") == {"native-w"}
