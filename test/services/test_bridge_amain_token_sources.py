"""cao-bridge names both token sources when neither is configured.

``_amain`` now accepts the token via ``CAO_RUNTIME_TOKEN`` or the owner-only
``CAO_RUNTIME_TOKEN_FILE``. When neither is set the startup error must name both
so an operator knows which knobs exist.
"""

import asyncio

import pytest


def test_amain_error_names_both_token_variables(monkeypatch):
    monkeypatch.setenv("CAO_BRIDGE_SERVER_URL", "ws://server/runtime/channel")
    monkeypatch.setenv("CAO_BRIDGE_RUNTIME_ID", "rt-1")
    monkeypatch.delenv("CAO_RUNTIME_TOKEN", raising=False)
    monkeypatch.delenv("CAO_RUNTIME_TOKEN_FILE", raising=False)

    import cli_agent_orchestrator.utils.runtime_token as rt

    rt._reset_cache_for_tests()

    from cli_agent_orchestrator.runtime_channel.bridge import _amain

    with pytest.raises(SystemExit) as exc:
        asyncio.run(_amain())

    msg = str(exc.value)
    assert "CAO_RUNTIME_TOKEN" in msg
    assert "CAO_RUNTIME_TOKEN_FILE" in msg


def test_amain_accepts_the_token_file(monkeypatch, tmp_path):
    """A file-only credential is a valid configured source."""
    token_file = tmp_path / "runtime-token"
    token_file.write_text("file-token")
    monkeypatch.setenv("CAO_BRIDGE_SERVER_URL", "ws://server/runtime/channel")
    monkeypatch.setenv("CAO_BRIDGE_RUNTIME_ID", "rt-1")
    monkeypatch.delenv("CAO_RUNTIME_TOKEN", raising=False)
    monkeypatch.setenv("CAO_RUNTIME_TOKEN_FILE", str(token_file))

    import cli_agent_orchestrator.utils.runtime_token as rt

    rt._reset_cache_for_tests()

    # The token gate must pass; stop right after by making the Bridge constructor
    # the first thing that raises, proving startup got past the credential check.
    import cli_agent_orchestrator.runtime_channel.bridge as bridge_mod

    def _boom(*_a, **_k):
        raise RuntimeError("reached bridge construction with a token")

    monkeypatch.setattr(bridge_mod, "init_runtime_db", lambda: None)
    monkeypatch.setattr(bridge_mod, "Bridge", _boom)

    with pytest.raises(RuntimeError, match="reached bridge construction"):
        asyncio.run(bridge_mod._amain())
