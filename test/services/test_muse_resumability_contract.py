"""Muse resumability contract: a bound Muse row has a version pin defined.

``muse_cli`` is a supported native-TUI provider, so a bound Muse row must
map to the ``muse`` version pin rather than falsely report "no version pin
is defined".  This only makes the row *structurally* resumable; the global
one-way refusal of the resume machinery remains truthful until M3-B.
"""

from cli_agent_orchestrator.services import session_resumability as sr


def test_muse_cli_has_a_version_pin_contract_name():
    """``muse_cli`` maps to the ``muse`` pin, so a bound row is not 'no pin'."""
    assert sr._CONTRACT_NAME["muse_cli"] == "muse"


def test_a_bound_muse_row_is_structurally_resumable_with_a_pinned_version():
    row = {
        "terminal_id": "muse-1",
        "provider": "muse_cli",
        "agent_profile": "reviewer",
        "native_session_id": "ebab9822-608f-470b-8b35-ada098e0cf29",
    }
    verdict = sr.worker_resumability(
        row, installed_versions={"muse_cli": "Muse Code 0.1.0 (0.1.0-R708.1)"}
    )
    assert verdict["resumable"] is True
    assert verdict["reason"] is None
