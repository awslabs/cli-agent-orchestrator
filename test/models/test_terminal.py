"""The response-model boundary a projected terminal has to cross.

``terminal_projection`` reports a provider status for a live pane and the
observed lifecycle for a row whose identity does not resolve — the two
facts share one field because there is never anything to report from both.
The model must admit exactly that union: while ``status`` validated against
the provider vocabulary alone, every non-live row failed response
validation on ``GET /terminals/{id}`` — and the ``ValidationError``, a
``ValueError``, surfaced as a 404, so a legacy ``unknown-liveness`` row
read as absent instead of as the historical record it is (cond-0173).

These tests pin both halves of the contract: the whole truthful vocabulary
validates, and an invented state is still refused — widening the boundary
must not become dropping it.
"""

import pytest
from pydantic import ValidationError

from cli_agent_orchestrator.models.terminal import Terminal, TerminalLifecycleState

PROVIDER_STATUSES = [
    "unknown",
    "idle",
    "processing",
    "completed",
    "waiting_user_answer",
    "error",
    "not_fifo_monitored",
]

LIFECYCLE_STATES = ["live", "superseded", "dead", "unknown-liveness"]


def _payload(status):
    return {
        "id": "aaaa1111",
        "name": "worker",
        "provider": "claude_code",
        "session_name": "cao-proj",
        "status": status,
    }


class TestStatusAdmitsTheTruthfulVocabulary:
    @pytest.mark.parametrize("status", PROVIDER_STATUSES)
    def test_provider_statuses_validate(self, status):
        terminal = Terminal(**_payload(status))
        assert terminal.status == status
        assert terminal.model_dump()["status"] == status

    @pytest.mark.parametrize("status", LIFECYCLE_STATES)
    def test_lifecycle_states_validate(self, status):
        terminal = Terminal(**_payload(status))
        assert terminal.status == status
        assert terminal.model_dump()["status"] == status

    def test_the_enum_mirrors_the_stored_lifecycle_vocabulary(self):
        """One vocabulary, spelled the same in both stores and on the wire."""
        assert {state.value for state in TerminalLifecycleState} == {
            "live",
            "superseded",
            "dead",
            "unknown-liveness",
        }


class TestStatusStillRefusesInvention:
    def test_an_invented_status_is_rejected(self):
        with pytest.raises(ValidationError):
            Terminal(**_payload("probably-alive"))

    def test_a_lifecycle_typo_is_rejected(self):
        with pytest.raises(ValidationError):
            Terminal(**_payload("unknown_liveness"))
