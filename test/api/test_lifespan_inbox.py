"""Tests for the inbox-watcher branch of the FastAPI lifespan.

The lifespan (api/main.py) starts one of two inbox watchers depending on the
active terminal backend:

- HerdrBackend  -> HerdrInboxService (socket events), scheduled as an asyncio task
- anything else -> PollingObserver (tmux log-file watchdog)

These tests pin both the startup wiring and the matching shutdown teardown for
each path. Everything external is mocked, so no herdr socket, watchdog thread,
database, or real background task is created.

Mocking notes:
- ``asyncio.create_task`` is replaced with a fake that returns a ``_FakeTask``.
  A real task can't be created from ``MagicMock().start()`` (not a coroutine),
  and we need to spy on ``.cancel()`` while still being awaitable for the
  ``await task`` in shutdown. ``_FakeTask`` raises ``CancelledError`` once
  cancelled, exercising the ``except asyncio.CancelledError`` branch.
- ``MagicMock(spec=HerdrBackend)`` satisfies ``isinstance(backend, HerdrBackend)``.
"""

import asyncio
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.api.main import app, lifespan
from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend
from cli_agent_orchestrator.constants import INBOX_POLLING_INTERVAL, TERMINAL_LOG_DIR
from cli_agent_orchestrator.plugins import PluginRegistry

# --- Test doubles -------------------------------------------------------


class _FakeTask:
    """Stand-in for asyncio.Task: awaitable and records ``cancel()`` calls.

    Mirrors the one behavior shutdown depends on: awaiting a task that has been
    cancelled raises ``CancelledError``. ``source`` keeps the original argument
    passed to ``create_task`` so a specific task can be located in assertions.
    """

    def __init__(self, source: object) -> None:
        self.source = source
        # Close real coroutines so Python does not warn "never awaited".
        if asyncio.iscoroutine(source):
            source.close()
        self.cancel = MagicMock(name="cancel")

    def __await__(self):
        async def _body() -> None:
            if self.cancel.called:
                raise asyncio.CancelledError()
            return None

        return _body().__await__()


def _make_fake_create_task(tasks: list) -> object:
    """Build a ``create_task`` replacement that records every created task."""

    def _fake_create_task(coro, *_args, **_kwargs) -> _FakeTask:
        task = _FakeTask(coro)
        tasks.append(task)
        return task

    return _fake_create_task


def _find_task(tasks: list, source: object) -> "_FakeTask | None":
    """Return the recorded task created from ``source``, or None."""
    for task in tasks:
        if task.source is source:
            return task
    return None


async def _fake_flow_daemon() -> None:
    """Replacement for flow_daemon so create_task gets a closeable coroutine."""


async def _fake_opencode_daemon(registry: object) -> None:
    """Replacement for opencode_inbox_delivery_daemon (takes a registry arg)."""
    del registry


@contextmanager
def _patched_lifespan(backend: object, tasks: list):
    """Patch every external dependency the lifespan touches at startup/shutdown.

    Yields a namespace of the mocks tests assert against.
    """
    with ExitStack() as stack:

        def patch_main(name: str, **kwargs):
            return stack.enter_context(patch(f"cli_agent_orchestrator.api.main.{name}", **kwargs))

        # Intercept task creation so no real background tasks run.
        stack.enter_context(patch("asyncio.create_task", _make_fake_create_task(tasks)))

        # No-op the side-effecting startup calls.
        patch_main("setup_logging")
        patch_main("init_db")
        patch_main("cleanup_old_data")
        patch_main("flow_daemon", new=_fake_flow_daemon)
        patch_main("opencode_inbox_delivery_daemon", new=_fake_opencode_daemon)

        namespace = SimpleNamespace(
            get_backend=patch_main("get_backend", return_value=backend),
            herdr_cls=patch_main("HerdrInboxService"),
            set_svc=patch_main("set_herdr_inbox_service"),
            observer_cls=patch_main("PollingObserver"),
            log_handler_cls=patch_main("LogFileHandler"),
            load=stack.enter_context(patch.object(PluginRegistry, "load", new_callable=AsyncMock)),
            teardown=stack.enter_context(
                patch.object(PluginRegistry, "teardown", new_callable=AsyncMock)
            ),
        )
        yield namespace


class TestLifespanInboxWiring:
    """Startup + shutdown wiring for both inbox-watcher backends."""

    @pytest.mark.asyncio
    async def test_herdr_backend_starts_service_and_sets_registry(self) -> None:
        """Herdr backend builds the service, registers it, and schedules start()."""
        # Arrange
        tasks: list = []
        backend = MagicMock(spec=HerdrBackend)
        backend.herdr_session = "cao"

        # Act
        with _patched_lifespan(backend, tasks) as mocks:
            async with lifespan(app):
                # Assert (startup)
                mocks.herdr_cls.assert_called_once_with(
                    herdr_session="cao",
                    delivery_callback=ANY,
                )
                # The delivery callback must be a callable closure.
                callback = mocks.herdr_cls.call_args.kwargs["delivery_callback"]
                assert callable(callback)

                svc = mocks.herdr_cls.return_value
                mocks.set_svc.assert_called_once_with(svc)

                # svc.start() was scheduled as a task.
                svc.start.assert_called_once()
                assert _find_task(tasks, svc.start.return_value) is not None

                # The tmux watcher path must not run.
                mocks.observer_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_tmux_backend_starts_polling_observer(self) -> None:
        """Non-herdr backend builds, schedules, and starts the PollingObserver."""
        # Arrange
        tasks: list = []
        backend = MagicMock()  # not a HerdrBackend instance

        # Act
        with _patched_lifespan(backend, tasks) as mocks:
            async with lifespan(app):
                # Assert (startup)
                mocks.observer_cls.assert_called_once_with(timeout=INBOX_POLLING_INTERVAL)
                observer = mocks.observer_cls.return_value

                # LogFileHandler is built with the registry stored on app.state...
                mocks.log_handler_cls.assert_called_once_with(app.state.plugin_registry)
                # ...and that handler is scheduled on the terminal log dir.
                observer.schedule.assert_called_once_with(
                    mocks.log_handler_cls.return_value,
                    str(TERMINAL_LOG_DIR),
                    recursive=False,
                )
                observer.start.assert_called_once()

                # The herdr path must not run.
                mocks.herdr_cls.assert_not_called()
                mocks.set_svc.assert_not_called()

    @pytest.mark.asyncio
    async def test_herdr_shutdown_cancels_task_and_skips_observer(self) -> None:
        """On exit the herdr path cancels its task; observer teardown never runs."""
        # Arrange
        tasks: list = []
        backend = MagicMock(spec=HerdrBackend)
        backend.herdr_session = "cao"

        # Act
        with _patched_lifespan(backend, tasks) as mocks:
            async with lifespan(app):
                svc = mocks.herdr_cls.return_value
            # Assert (shutdown — after context exit)
            herdr_task = _find_task(tasks, svc.start.return_value)
            assert herdr_task is not None
            herdr_task.cancel.assert_called_once()

            # Observer was never constructed, so stop()/join() cannot run.
            mocks.observer_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_tmux_shutdown_stops_observer_and_skips_herdr_task(self) -> None:
        """On exit the tmux path stops+joins the observer; no herdr task exists."""
        # Arrange
        tasks: list = []
        backend = MagicMock()  # not a HerdrBackend instance

        # Act
        with _patched_lifespan(backend, tasks) as mocks:
            async with lifespan(app):
                observer = mocks.observer_cls.return_value
            # Assert (shutdown — after context exit)
            observer.stop.assert_called_once()
            observer.join.assert_called_once()

            # No herdr inbox service or task was ever created to cancel.
            mocks.herdr_cls.assert_not_called()
            assert _find_task(tasks, mocks.herdr_cls.return_value.start.return_value) is None
