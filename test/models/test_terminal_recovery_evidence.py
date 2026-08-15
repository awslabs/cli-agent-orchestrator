"""The HTTP Terminal response must not strip status/recovery evidence."""

from cli_agent_orchestrator.models.terminal import Terminal


def test_terminal_model_preserves_fusion_and_recovery_contract():
    recovery = {
        "schema": "cao.provider-recovery-evidence.v1",
        "occurrence_id": "occurrence-1",
        "agent_id": None,
        "incarnation_id": None,
        "detector": "cao-provider-terminal-error",
        "detector_version": "1",
        "pattern": "claude.connection-closed-mid-response",
        "terminal_id": "abcd1234",
        "generation": "generation-1",
        "native_session_id": "native-1",
        "provider": "claude_code",
        "provider_version": "2.1.220",
        "turn_state": "terminal",
        "recovery_action": "nudge",
        "raw_text": "API Error: Connection closed mid-response.",
        "raw_sha256": "a" * 64,
        "raw_text_truncated": False,
        "confidence": "high",
        "reason": "locally proven exact line",
        "signals": [],
        "opened_at": "2026-08-15T00:00:00Z",
    }
    terminal = Terminal(
        id="abcd1234",
        name="worker",
        provider="claude_code",
        session_name="cao-test",
        status="error",
        status_confidence="high",
        status_reason="provider recovery evidence",
        status_signals=[{"name": "screen", "state": "available", "value": "error"}],
        wedged=False,
        recovery_evidence=recovery,
    )

    published = terminal.model_dump(mode="json", by_alias=True)
    assert published["status_confidence"] == "high"
    assert published["status_reason"] == "provider recovery evidence"
    assert published["status_signals"] == [
        {"name": "screen", "state": "available", "value": "error"}
    ]
    assert published["wedged"] is False
    assert published["recovery_evidence"] == recovery
