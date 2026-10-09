"""session_service.delete_session only ever tears down cao- sessions."""

from unittest.mock import patch

import pytest

from cli_agent_orchestrator.services.session_service import delete_session


def test_delete_session_refuses_unprefixed_name_before_touching_anything():
    with (
        patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session") as rows,
        patch("cli_agent_orchestrator.services.session_service.get_backend") as backend,
    ):
        with pytest.raises(ValueError, match="not a CAO session name"):
            delete_session("dev")
    rows.assert_not_called()
    backend.assert_not_called()
