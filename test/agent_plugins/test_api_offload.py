"""Plugin HTTP operations must not run on the event loop.

Reproduced by review on #584: the plugin endpoints were ``async def`` but ran the
synchronous resolve/validate/publish/refresh pipeline inline. A git source can
block in ``subprocess.run`` for up to 300 seconds and a large local source blocks
in ``copytree``; for that whole window the server serves no health or session
request and runs no status or inbox task.

Two independent assertions, because each catches something the other does not:

* **Thread identity** — the blocking callable must execute on a *different*
  thread from the coroutine that scheduled it. Deterministic, no timing.
* **Loop responsiveness** — a second request must complete *while* a slow plugin
  operation is still in flight. This is the property an operator actually cares
  about, and it is asserted against the real ASGI app.

Both are mutation-verified: reverting the ``asyncio.to_thread`` offload fails
them.
"""

from __future__ import annotations

import asyncio
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from .conftest import build_plugin


@pytest.fixture
def offload_client(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "agent-plugins"
    data_dir = tmp_path / "agent-plugin-data"
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    monkeypatch.setattr("cli_agent_orchestrator.agent_plugins.store.AGENT_PLUGINS_DIR", plugins_dir)
    monkeypatch.setattr(
        "cli_agent_orchestrator.agent_plugins.store.AGENT_PLUGIN_DATA_DIR", data_dir
    )
    monkeypatch.setattr("cli_agent_orchestrator.agent_plugins.projection.SKILLS_DIR", skills_dir)
    monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", skills_dir)
    monkeypatch.setattr(
        "cli_agent_orchestrator.agent_plugins.installer._refresh_agent_artifacts", lambda: None
    )

    from cli_agent_orchestrator.api.main import app

    return TestClient(app, base_url="http://localhost")


class TestBlockingWorkLeavesTheEventLoopThread:
    """The pipeline must run on a worker thread, not the loop's own thread."""

    def _thread_probe(self, monkeypatch, target: str):
        """Record the thread of the coroutine vs. the thread of the blocking call.

        ``_plugin_source(body)`` is evaluated in the handler coroutine itself, so
        it samples the *event loop* thread; the patched pipeline function samples
        wherever the blocking work actually ran. Comparing the two needs no
        sleeps and cannot flake.
        """
        seen: dict[str, int] = {}
        real_source = getattr(
            __import__("cli_agent_orchestrator.api.main", fromlist=["_plugin_source"]),
            "_plugin_source",
        )

        def probe_source(body):
            seen["loop"] = threading.get_ident()
            return real_source(body)

        def probe_target(*args, **kwargs):
            seen["work"] = threading.get_ident()
            raise RuntimeError("stop here — thread identity is all this test needs")

        monkeypatch.setattr("cli_agent_orchestrator.api.main._plugin_source", probe_source)
        monkeypatch.setattr(target, probe_target)
        return seen

    def test_install_runs_off_the_loop_thread(self, offload_client, monkeypatch, tmp_path):
        seen = self._thread_probe(
            monkeypatch, "cli_agent_orchestrator.agent_plugins.installer.install"
        )
        pkg = build_plugin(tmp_path / "pkg", name="demo", skills=("alpha",))

        offload_client.post("/plugins", json={"source": str(pkg)})

        assert "loop" in seen and "work" in seen
        assert seen["work"] != seen["loop"], "install ran on the event loop thread"

    def test_validate_runs_off_the_loop_thread(self, offload_client, monkeypatch, tmp_path):
        seen = self._thread_probe(
            monkeypatch, "cli_agent_orchestrator.agent_plugins.installer.validate_source"
        )
        pkg = build_plugin(tmp_path / "pkg2", name="demo", skills=("alpha",))

        offload_client.post("/plugins/validate", json={"source": str(pkg)})

        assert seen["work"] != seen["loop"], "validate ran on the event loop thread"

    def test_uninstall_runs_off_the_loop_thread(self, offload_client, monkeypatch):
        seen: dict[str, int] = {}

        def probe(*args, **kwargs):
            seen["work"] = threading.get_ident()
            raise RuntimeError("stop")

        monkeypatch.setattr("cli_agent_orchestrator.agent_plugins.installer.uninstall", probe)

        # The loop thread is sampled from a plain request that does no offloading.
        loop_probe: dict[str, int] = {}
        real_warn = getattr(
            __import__("cli_agent_orchestrator.api.main", fromlist=["_with_untrusted_warning"]),
            "_with_untrusted_warning",
        )

        def probe_warn(payload):
            loop_probe["loop"] = threading.get_ident()
            return real_warn(payload)

        monkeypatch.setattr("cli_agent_orchestrator.api.main._with_untrusted_warning", probe_warn)
        offload_client.get("/plugins")
        offload_client.delete("/plugins/demo")

        assert seen["work"] != loop_probe["loop"], "uninstall ran on the event loop thread"


@pytest.mark.asyncio
async def test_the_loop_still_serves_requests_during_a_slow_plugin_install(
    tmp_path, monkeypatch
) -> None:
    """A slow install must not freeze the orchestrator.

    The reviewer's concrete complaint: during a 300s git clone "the server loop
    cannot service health/session requests or run status and inbox tasks".

    Asserted by **wall clock**, not by a timeout. A frozen event loop cannot run
    its own timer callbacks, so `asyncio.wait_for` around the probe request would
    be unenforceable in exactly the failure mode under test — it would sit
    blocked, then succeed once the block ended, and pass. Measuring elapsed time
    across the whole interaction is the assertion that actually discriminates:
    with the work on a worker thread the probe returns in milliseconds; with the
    work inline the probe cannot return until the block has run its full course.
    """
    plugins_dir = tmp_path / "agent-plugins"
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    monkeypatch.setattr("cli_agent_orchestrator.agent_plugins.store.AGENT_PLUGINS_DIR", plugins_dir)
    monkeypatch.setattr(
        "cli_agent_orchestrator.agent_plugins.store.AGENT_PLUGIN_DATA_DIR",
        tmp_path / "agent-plugin-data",
    )
    monkeypatch.setattr("cli_agent_orchestrator.agent_plugins.projection.SKILLS_DIR", skills_dir)
    monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", skills_dir)

    BLOCK_SECONDS = 6.0
    released = threading.Event()
    entered = threading.Event()

    def slow_install(*args, **kwargs):
        entered.set()
        # Blocks the calling thread, exactly as `subprocess.run`/`copytree` do.
        released.wait(timeout=BLOCK_SECONDS)
        raise RuntimeError("released")

    monkeypatch.setattr("cli_agent_orchestrator.agent_plugins.installer.install", slow_install)

    from cli_agent_orchestrator.api.main import app

    pkg = build_plugin(tmp_path / "pkg", name="demo", skills=("alpha",))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        started = time.monotonic()
        install_task = asyncio.create_task(client.post("/plugins", json={"source": str(pkg)}))

        # Wait for the blocking call to be genuinely in flight. When the work is
        # offloaded the loop is free, so this settles in milliseconds. When it is
        # inline this loop cannot run at all until the block has finished — which
        # is precisely what the elapsed-time assertion below then catches.
        for _ in range(300):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set(), "the slow install never started, so nothing was proven"

        probe = await client.get("/plugins")
        elapsed = time.monotonic() - started

        assert probe.status_code == 200
        # Served promptly => the block was not on the loop. Comfortably below
        # BLOCK_SECONDS so a slow CI machine cannot flake it, yet far below what
        # an inline block costs.
        assert elapsed < BLOCK_SECONDS / 2, (
            f"the event loop was blocked for {elapsed:.2f}s during a plugin install; "
            f"plugin work is not running off the loop"
        )

        released.set()
        # The handler maps the raised error to a 500 rather than propagating it,
        # so the task resolves to a response.
        install_response = await install_task
        assert install_response.status_code == 500
