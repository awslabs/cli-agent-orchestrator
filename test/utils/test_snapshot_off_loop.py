"""AG-UI fleet snapshots are taken off the event loop (#745).

Under the remote backend a snapshot is a round trip to each runner. Taken on
the loop thread, it would stop every other request for that long.
"""

import ast
import asyncio
import threading
import time
from pathlib import Path

import cli_agent_orchestrator
from cli_agent_orchestrator.utils.event import snapshot_off_loop

SRC = Path(cli_agent_orchestrator.__file__).parent
# Each AG-UI stream's snapshot function, by the module that streams it.
SNAPSHOT_CALLERS = {
    "api/main.py": "_fleet_snapshot",
    "services/agui/ambient_run.py": "fleet_snapshot",
    "services/agui/run_plane.py": "snapshot_fn",
}


def test_the_snapshot_runs_in_a_worker_and_the_loop_keeps_going():
    loop_thread = []
    snapshot_thread = []
    ticks = []

    def slow_snapshot():
        snapshot_thread.append(threading.get_ident())
        time.sleep(0.5)
        return {"fleet": 1}

    async def run():
        loop_thread.append(threading.get_ident())

        async def tick():
            while True:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.05)

        ticker = asyncio.create_task(tick())
        result = await snapshot_off_loop(slow_snapshot)
        ticker.cancel()
        return result

    assert asyncio.run(run()) == {"fleet": 1}
    assert snapshot_thread[0] != loop_thread[0]
    assert len(ticks) >= 5  # the loop ran while the snapshot slept


def test_no_async_stream_takes_its_snapshot_on_the_loop():
    """Every async call of a stream's snapshot function goes through
    ``snapshot_off_loop``, which receives the function, not its result."""
    found = {}
    for rel, name in SNAPSHOT_CALLERS.items():
        tree = ast.parse((SRC / rel).read_text(encoding="utf-8"))
        wrapped = direct = 0

        def visit(node, in_async):
            nonlocal wrapped, direct
            if isinstance(node, (ast.FunctionDef, ast.Lambda)):
                in_async = False
            elif isinstance(node, ast.AsyncFunctionDef):
                in_async = True
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "snapshot_off_loop" and any(
                    isinstance(a, ast.Name) and a.id == name for a in node.args
                ):
                    wrapped += 1
                elif node.func.id == name and in_async:
                    direct += 1
            for child in ast.iter_child_nodes(node):
                visit(child, in_async)

        visit(tree, False)
        found[rel] = (wrapped, direct)
    assert found == {rel: (2, 0) for rel in SNAPSHOT_CALLERS}
