"""Bounded asynchronous shadow tasks that never gate launch completion."""

import asyncio
import time
from typing import Any, Awaitable, Callable

from cli_agent_orchestrator.decisions.store import DecisionStore


class ShadowRunner:
    def __init__(
        self, store: DecisionStore, *, max_concurrent: int = 4, max_pending: int = 64
    ) -> None:
        self.store = store
        self.max_pending = max_pending
        self._sem = asyncio.Semaphore(max_concurrent)
        self._tasks: dict[asyncio.Task[None], int] = {}
        self._closing = False

    def submit(self, record_id: int, work: Callable[[], Awaitable[dict[str, Any]]]) -> bool:
        if self._closing or len(self._tasks) >= self.max_pending:
            self.store.update_decision(
                record_id,
                dict(outcome="shadow", reason="shadow_dropped", decision_status="dropped"),
                emit=False,
            )
            return False
        task = asyncio.create_task(self._run(record_id, work))
        self.store.shadow_started(record_id)
        self._tasks[task] = record_id
        task.add_done_callback(lambda finished: self._tasks.pop(finished, None))
        return True

    async def _run(self, record_id: int, work: Callable[[], Awaitable[dict[str, Any]]]) -> None:
        started: float | None = None
        try:
            async with self._sem:
                started = time.perf_counter()
                values = await work()
            values.update(outcome="shadow", applied_value=None, decision_status="done")
            self.store.update_decision(record_id, values)
        except asyncio.CancelledError:
            self.store.update_decision(
                record_id, dict(outcome="shadow", decision_status="interrupted")
            )
            raise
        except Exception:
            self.store.update_decision(
                record_id,
                dict(
                    outcome="shadow",
                    reason="error",
                    decision_status="done",
                    latency_ms=(
                        (time.perf_counter() - started) * 1000 if started is not None else 0.0
                    ),
                ),
            )

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def close(self) -> None:
        self._closing = True
        tasks = tuple(self._tasks)
        for task in tasks:
            self.store.update_decision(
                self._tasks[task], dict(outcome="shadow", decision_status="interrupted")
            )
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=0.5)
