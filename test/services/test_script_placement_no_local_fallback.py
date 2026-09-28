"""A configured script runtime never falls back to a local subprocess.

When ``CAO_SCRIPT_RUNTIME`` is set the script ALWAYS runs in that runtime; a
disconnected runtime fails the run rather than silently spawning author code
beside the central server. This pins the scenario — runtime
configured, disconnected -> FAILED, and no local subprocess is spawned — so the
misleading "when it names a connected runtime" wording cannot regress into an
actual local fallback.
"""

from types import SimpleNamespace

import pytest

from cli_agent_orchestrator.models.workflow_runtime import RunState
from cli_agent_orchestrator.runtime_channel.registry import runtime_registry
from cli_agent_orchestrator.services import script_runner


def _record():
    return SimpleNamespace(run_id="run-1", step_id="step-1", remote_script=None)


@pytest.mark.asyncio
async def test_a_disconnected_configured_runtime_fails_without_a_local_subprocess(
    monkeypatch, tmp_path
):
    script = tmp_path / "wf.py"
    script.write_text("print('should never run locally')\n")

    # An operator has pinned a runtime, but its channel is down.
    monkeypatch.setattr(script_runner, "_remote_script_runtime", lambda: "worker-gone")
    monkeypatch.setattr(runtime_registry, "get_runtime", lambda rid: None)

    spawned = {"count": 0}

    async def _spy_spawn(*a, **k):
        spawned["count"] += 1
        raise AssertionError("a configured runtime must not spawn a local subprocess")

    monkeypatch.setattr(script_runner.asyncio, "create_subprocess_exec", _spy_spawn)

    captured = {}

    async def fake_finalize(record, **k):
        captured.update(k)
        return SimpleNamespace(state=RunState.FAILED, kind="error")

    monkeypatch.setattr(script_runner, "_finalize", fake_finalize)

    result = await script_runner._drive_process(_record(), str(script), {})

    assert result.state == RunState.FAILED
    assert spawned["count"] == 0
    assert "is not connected" in (captured.get("error") or "")
