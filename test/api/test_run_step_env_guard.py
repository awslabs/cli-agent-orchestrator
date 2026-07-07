"""Tests for the run-step env-var guard (issue #312, Bolt 2 / U2, C6).

Every request-shape violation surfaces as FastAPI's standard 422 envelope
(Q2=A — validators raise ValueError, no HTTPException in U2 code). Error
bodies name the KEY and the rule only; the supplied VALUE is never echoed
(NFR-SEC-4 sanitized-error rule — pinned by the sentinel tests, one per
validator arm).
"""

from unittest.mock import AsyncMock, patch

from cli_agent_orchestrator.constants import TERMINALS_RUN_STEP_ROUTE
from cli_agent_orchestrator.models.terminal import AgentStepResult, TerminalStatus

_RUN_STEP = "cli_agent_orchestrator.api.main.run_agent_step"

_ALL_KEYS = {
    "CAO_WORKFLOW_RUN_ID": "run-abc123",
    "CAO_WORKFLOW_STEP_ID": "step-1",
    "CAO_WORKFLOW_GENERATION": "2",
}


def _body(**overrides):
    base = {"provider": "kiro_cli", "agent": "developer", "prompt": "do it"}
    base.update(overrides)
    return base


def _ok_result():
    return AgentStepResult(
        terminal_id="abc12345", last_message="done", status=TerminalStatus.COMPLETED
    )


class TestForwarding:
    def test_all_three_allowlisted_keys_forward_to_run_agent_step(self, client):
        with patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())) as m_run:
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(env_vars=_ALL_KEYS))
        assert resp.status_code == 200
        # BR-14: forwarded verbatim — no rewriting, no defaults, no merging.
        assert m_run.await_args.kwargs["env_vars"] == _ALL_KEYS

    def test_absent_env_vars_preserves_base_behavior(self, client):
        # BR-15: omitted field forwards None — existing callers unaffected.
        with patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())) as m_run:
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body())
        assert resp.status_code == 200
        assert m_run.await_args.kwargs["env_vars"] is None

    def test_run_id_with_generation_but_no_step_id_is_allowed(self, client):
        # BR-7: RUN_ID without STEP_ID is a valid run-row-level call.
        env = {"CAO_WORKFLOW_RUN_ID": "run-abc", "CAO_WORKFLOW_GENERATION": "1"}
        with patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(env_vars=env))
        assert resp.status_code == 200

    def test_happy_path_values_at_the_64_char_regex_ceiling(self, client):
        # The effective accepted value length is 64 (WORKFLOW_NAME_RE).
        env = {
            "CAO_WORKFLOW_RUN_ID": "r" * 64,
            "CAO_WORKFLOW_GENERATION": "g" * 64,
        }
        with patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(env_vars=env))
        assert resp.status_code == 200


class TestPerKeyRejections:
    def test_non_allowlisted_key_is_422_naming_key_and_allowlist(self, client):
        resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(env_vars={"PATH": "/usr/bin"}))
        assert resp.status_code == 422
        body = resp.text
        assert "PATH" in body
        assert "CAO_WORKFLOW_RUN_ID" in body  # allowlist named for debuggability

    def test_value_over_256_cap_is_422(self, client):
        env = dict(_ALL_KEYS, CAO_WORKFLOW_RUN_ID="x" * 300)
        resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(env_vars=env))
        assert resp.status_code == 422
        assert "256" in resp.text

    def test_value_over_64_chars_rejected_by_regex_arm(self, client):
        # Under the 256 cap but over WORKFLOW_NAME_RE's 64 — the shared
        # validator arm fires.
        env = dict(_ALL_KEYS, CAO_WORKFLOW_RUN_ID="x" * 65)
        resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(env_vars=env))
        assert resp.status_code == 422

    def test_control_character_value_is_422(self, client):
        env = dict(_ALL_KEYS, CAO_WORKFLOW_RUN_ID="run\x1b[31m")
        resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(env_vars=env))
        assert resp.status_code == 422
        assert "control characters" in resp.text

    def test_traversal_token_value_is_422(self, client):
        env = dict(_ALL_KEYS, CAO_WORKFLOW_RUN_ID="..")
        resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(env_vars=env))
        assert resp.status_code == 422


class TestCrossFieldRejections:
    def test_run_id_without_generation_is_422(self, client):
        env = {"CAO_WORKFLOW_RUN_ID": "run-abc"}
        resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(env_vars=env))
        assert resp.status_code == 422
        assert "CAO_WORKFLOW_GENERATION" in resp.text

    def test_generation_without_run_id_is_422(self, client):
        # Symmetric direction (BR-6): an unanchored generation token would
        # silently no-op the fence.
        env = {"CAO_WORKFLOW_GENERATION": "2"}
        resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(env_vars=env))
        assert resp.status_code == 422
        assert "CAO_WORKFLOW_RUN_ID" in resp.text

    def test_step_id_without_run_id_is_422(self, client):
        env = {"CAO_WORKFLOW_STEP_ID": "step-1"}
        resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(env_vars=env))
        assert resp.status_code == 422
        assert "CAO_WORKFLOW_RUN_ID" in resp.text

    def test_env_vars_with_reuse_terminal_id_is_422(self, client):
        # BR-8: env injection is ignored on reused terminals — silently
        # dropping fence tokens is a Forbidden silent failure.
        resp = client.post(
            TERMINALS_RUN_STEP_ROUTE,
            json=_body(env_vars=_ALL_KEYS, reuse_terminal_id="abc12345"),
        )
        assert resp.status_code == 422
        assert "reused terminal" in resp.text

    def test_empty_env_vars_with_reuse_terminal_id_is_allowed(self, client):
        # BR-8 fires on NON-EMPTY env_vars only — {} drops nothing.
        with patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())):
            resp = client.post(
                TERMINALS_RUN_STEP_ROUTE,
                json=_body(env_vars={}, reuse_terminal_id="abc12345"),
            )
        assert resp.status_code == 200


class TestSentinelNeverEchoed:
    """One sentinel test per validator arm that sees a VALUE: the supplied
    value must never appear anywhere in a 422 body (NFR-SEC-4)."""

    SENTINEL = "SENTINEL_zzz"

    def _assert_422_without_sentinel(self, client, env, value_sentinel):
        resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(env_vars=env))
        assert resp.status_code == 422
        assert value_sentinel not in resp.text

    def test_cap_arm_never_echoes_value(self, client):
        value = self.SENTINEL + "x" * 300
        env = dict(_ALL_KEYS, CAO_WORKFLOW_RUN_ID=value)
        self._assert_422_without_sentinel(client, env, self.SENTINEL)

    def test_control_char_arm_never_echoes_value(self, client):
        value = self.SENTINEL + "\x07"
        env = dict(_ALL_KEYS, CAO_WORKFLOW_RUN_ID=value)
        self._assert_422_without_sentinel(client, env, self.SENTINEL)

    def test_shared_validator_arm_never_echoes_value(self, client):
        # Invalid chars for WORKFLOW_NAME_RE but no control chars, under cap —
        # only the wrapped _validate_key_part arm fires. Its native message
        # interpolates the value; the wrapper must have stripped it.
        value = self.SENTINEL + "/../etc"
        env = dict(_ALL_KEYS, CAO_WORKFLOW_RUN_ID=value)
        self._assert_422_without_sentinel(client, env, self.SENTINEL)

    def test_allowlist_arm_never_echoes_value(self, client):
        env = {"NOT_ALLOWED": self.SENTINEL}
        self._assert_422_without_sentinel(client, env, self.SENTINEL)

    def test_cross_field_arm_never_echoes_value(self, client):
        # Model-validator errors anchor at ("body",) with the WHOLE request
        # body echoed as input — the redaction handler's `"env_vars" in
        # err["input"]` branch must drop it. The value passes every per-key
        # arm (fits WORKFLOW_NAME_RE) so ONLY the cross-field validator fires;
        # a loc-only redaction "simplification" would leak it.
        env = {"CAO_WORKFLOW_RUN_ID": "SENTINELzzz"}
        self._assert_422_without_sentinel(client, env, "SENTINELzzz")

    def test_reuse_terminal_arm_never_echoes_value(self, client):
        # Same whole-body-echo shape via the BR-8 model validator.
        env = dict(_ALL_KEYS, CAO_WORKFLOW_RUN_ID="SENTINELzzz")
        resp = client.post(
            TERMINALS_RUN_STEP_ROUTE,
            json=_body(env_vars=env, reuse_terminal_id="abc12345"),
        )
        assert resp.status_code == 422
        assert "SENTINELzzz" not in resp.text
