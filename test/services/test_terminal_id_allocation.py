"""Terminal ids must not be reissued while retained rows still reference them (#1088).

``generate_terminal_id`` mints 8 hex characters (32 bits), so two creates can
draw the same id. The ``terminals`` primary key only rejects that while both
rows are live; after deletion the id is free to be reissued, and two retained
tables still key on it:

- ``idempotency_keys`` resolves a caller's key to a terminal id and then looks
  the ROW up, so a reissued id reattaches a retry to an unrelated terminal.
- ``inbox`` joins a PENDING message's receiver id to ``terminals``, so a
  reissued id turns a deleted receiver's queued message into a delivery for
  the new terminal's live session.

Each of these tests fails on the pre-fix code.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    InboxModel,
    TerminalModel,
)
from cli_agent_orchestrator.clients.database import create_terminal as db_create_terminal
from cli_agent_orchestrator.clients.database import delete_terminal as db_delete_terminal
from cli_agent_orchestrator.clients.database import (
    fail_orphaned_pending_messages,
    list_pending_receiver_ids_older_than,
)
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.services import terminal_service


def _seed_stale_pending_message(receiver_id: str, message: str = "stale") -> None:
    """Seed an old PENDING message to a terminal row, then delete that row."""
    with database.SessionLocal() as db:
        db.add(
            TerminalModel(
                id=receiver_id,
                tmux_session="cao-old",
                tmux_window="old-window",
                provider="kiro_cli",
            )
        )
        db.add(
            InboxModel(
                sender_id="sender01",
                receiver_id=receiver_id,
                message=message,
                status=MessageStatus.PENDING.value,
                created_at=datetime.now(timezone.utc) - timedelta(seconds=120),
            )
        )
        db.commit()
    assert db_delete_terminal(receiver_id, end_reason="deleted") is True


@pytest.mark.usefixtures("isolated_memory_db")
class TestTerminalIdInUse:
    """The database predicate the allocator consults."""

    def test_live_terminal_claims_its_id(self):
        db_create_terminal("live1234", "cao-s", "w", "kiro_cli")

        assert database.terminal_id_in_use("live1234") is True

    def test_unreferenced_id_is_free(self):
        assert database.terminal_id_in_use("free1234") is False

    def test_idempotency_mapping_keeps_a_deleted_terminals_id_reserved(self):
        db_create_terminal(
            "gone1234",
            "cao-s",
            "w",
            "kiro_cli",
            idempotency_key="key-1",
            request_fingerprint="fp-1",
        )
        db_delete_terminal("gone1234", end_reason="deleted")

        assert database.terminal_id_in_use("gone1234") is True

    def test_pending_message_keeps_a_deleted_terminals_id_reserved(self):
        _seed_stale_pending_message("gone5678")

        assert database.terminal_id_in_use("gone5678") is True


@patch("cli_agent_orchestrator.services.terminal_service.terminal_id_in_use")
@patch("cli_agent_orchestrator.services.terminal_service.generate_terminal_id")
class TestAllocateTerminalId:
    """The retry loop around the generator."""

    def test_no_collision_keeps_the_first_id(self, mock_generate, mock_in_use):
        mock_generate.return_value = "fresh001"
        mock_in_use.return_value = False

        assert terminal_service._allocate_terminal_id() == "fresh001"
        mock_generate.assert_called_once_with()

    def test_retries_past_a_referenced_id(self, mock_generate, mock_in_use):
        mock_generate.side_effect = ["taken001", "fresh002"]
        mock_in_use.side_effect = [True, False]

        assert terminal_service._allocate_terminal_id() == "fresh002"
        assert mock_generate.call_count == 2

    def test_exhaustion_raises_instead_of_looping(self, mock_generate, mock_in_use):
        mock_generate.return_value = "taken001"
        mock_in_use.return_value = True

        with pytest.raises(RuntimeError, match="Could not allocate a terminal id"):
            terminal_service._allocate_terminal_id()

        assert mock_generate.call_count == terminal_service._TERMINAL_ID_ALLOCATION_ATTEMPTS


# ``kiro_cli`` is a stand-in here: no agent file is installed for ``developer``,
# and the provider double reports a passed identity check (#1074).
@pytest.mark.usefixtures("isolated_memory_db", "kiro_agent_store_not_under_test")
class TestCreateTerminalAvoidsReissuedIds:
    """The acceptance scenario end to end: a queued message must not follow the id."""

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.FIFO_DIR")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_window_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_session_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_terminal_id")
    @patch("cli_agent_orchestrator.services.terminal_service.agent_profiles.load_agent_profile")
    async def test_new_terminal_gets_a_fresh_id_and_the_queued_message_stays_orphaned(
        self,
        mock_load_profile,
        mock_gen_id,
        mock_gen_session,
        mock_gen_window,
        mock_backend,
        mock_provider_manager,
        mock_fifo_dir,
        mock_fifo_manager,
        mock_status_monitor,
    ):
        _seed_stale_pending_message("aaaaaaaa")

        # The generator's first draw is the deleted terminal's id. Without the
        # allocator this is the id the new terminal would launch and persist.
        mock_gen_id.side_effect = ["aaaaaaaa", "bbbbbbbb"]
        mock_gen_session.return_value = "cao-new"
        mock_gen_window.return_value = "developer-abcd"
        mock_backend.session_exists.return_value = False
        mock_load_profile.return_value = AgentProfile(name="developer", description="Developer")
        provider = AsyncMock()
        provider.initialize.return_value = True
        provider.identity_verified = True
        mock_provider_manager.create_provider.return_value = provider
        mock_fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")

        terminal = await terminal_service.create_terminal("kiro_cli", "developer", new_session=True)

        assert terminal.id == "bbbbbbbb"
        assert database.get_terminal_metadata("aaaaaaaa") is None
        assert database.get_terminal_metadata("bbbbbbbb") is not None

        # Reconciliation sees no live receiver for the queued message, fails it,
        # and never routes it into the terminal that was just created.
        assert list_pending_receiver_ids_older_than(30) == []
        assert len(fail_orphaned_pending_messages(30)) == 1
        with database.SessionLocal() as db:
            stale = db.query(InboxModel).filter(InboxModel.receiver_id == "aaaaaaaa").one()
            assert stale.status == MessageStatus.FAILED.value
