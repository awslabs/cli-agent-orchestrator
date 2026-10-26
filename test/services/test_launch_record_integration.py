"""Issue #1082: create_terminal writes one launch record on success, a record
write/finalize failure never fails or alters a launch or teardown, and
target_kind is classified correctly (installed / ephemeral / workflow)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.services.terminal_service import create_terminal

pytestmark = pytest.mark.usefixtures("isolated_memory_db")


def _patch_create_terminal_collaborators(test_fn):
    """Shared decorator stack for a successful create_terminal call, mirroring
    TestCreateTerminal in test_terminal_service_full.py. ``db_create_launch_record``
    is deliberately NOT mocked here -- these tests assert against the real
    in-memory DB via ``isolated_memory_db``."""
    decorators = [
        patch("cli_agent_orchestrator.services.terminal_service.delete_terminals_by_session"),
        patch("cli_agent_orchestrator.services.terminal_service.status_monitor"),
        patch("cli_agent_orchestrator.services.terminal_service.fifo_manager"),
        patch("cli_agent_orchestrator.services.terminal_service.FIFO_DIR"),
        patch("cli_agent_orchestrator.services.terminal_service.provider_manager"),
        patch("cli_agent_orchestrator.services.terminal_service.db_create_terminal"),
        patch("cli_agent_orchestrator.backends.registry._backend"),
        patch("cli_agent_orchestrator.services.terminal_service.generate_window_name"),
        patch("cli_agent_orchestrator.services.terminal_service.generate_session_name"),
        patch("cli_agent_orchestrator.services.terminal_service.generate_terminal_id"),
        patch("cli_agent_orchestrator.services.terminal_service.agent_profiles.load_agent_profile"),
    ]
    for decorator in reversed(decorators):
        test_fn = decorator(test_fn)
    return test_fn


def _wire_common_mocks(
    mock_load_profile,
    mock_gen_id,
    mock_gen_session,
    mock_gen_window,
    mock_tmux,
    mock_db_create,
    mock_provider_manager,
    mock_fifo_dir,
    *,
    profile=None,
):
    mock_gen_id.return_value = "test1234"
    mock_gen_session.return_value = "cao-session"
    mock_gen_window.return_value = "developer-abcd"
    mock_tmux.session_exists.return_value = False
    mock_load_profile.return_value = profile or AgentProfile(
        name="developer", description="Developer"
    )
    mock_provider = AsyncMock()
    mock_provider.initialize.return_value = True
    mock_provider_manager.create_provider.return_value = mock_provider
    mock_fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")
    mock_db_create.return_value = {"ephemeral": False}


class TestLaunchRecordOnCreate:
    @pytest.mark.asyncio
    @_patch_create_terminal_collaborators
    async def test_installed_profile_gets_launch_record(
        self,
        mock_load_profile,
        mock_gen_id,
        mock_gen_session,
        mock_gen_window,
        mock_tmux,
        mock_db_create,
        mock_provider_manager,
        mock_fifo_dir,
        mock_fifo_manager,
        mock_status_monitor,
        mock_delete_terminals_by_session,
    ):
        from cli_agent_orchestrator.clients import database

        _wire_common_mocks(
            mock_load_profile,
            mock_gen_id,
            mock_gen_session,
            mock_gen_window,
            mock_tmux,
            mock_db_create,
            mock_provider_manager,
            mock_fifo_dir,
        )

        result = await create_terminal("kiro_cli", "developer", new_session=True)

        record = database.get_launch_record(result.id)
        assert record is not None
        assert record["session_name"] == "cao-session"
        assert record["provider"] == "kiro_cli"
        assert record["target_name"] == "developer"
        assert record["target_kind"] == "installed"
        assert record["ended_at"] is None

    @pytest.mark.asyncio
    @_patch_create_terminal_collaborators
    async def test_workflow_env_var_classifies_as_workflow(
        self,
        mock_load_profile,
        mock_gen_id,
        mock_gen_session,
        mock_gen_window,
        mock_tmux,
        mock_db_create,
        mock_provider_manager,
        mock_fifo_dir,
        mock_fifo_manager,
        mock_status_monitor,
        mock_delete_terminals_by_session,
    ):
        """Mutation guard: target_kind must come from CAO_WORKFLOW_RUN_ID presence,
        not merely from being an installed profile."""
        from cli_agent_orchestrator.clients import database

        _wire_common_mocks(
            mock_load_profile,
            mock_gen_id,
            mock_gen_session,
            mock_gen_window,
            mock_tmux,
            mock_db_create,
            mock_provider_manager,
            mock_fifo_dir,
        )

        result = await create_terminal(
            "kiro_cli",
            "developer",
            new_session=True,
            env_vars={"CAO_WORKFLOW_RUN_ID": "run-1", "CAO_WORKFLOW_STEP_ID": "step-1"},
        )

        record = database.get_launch_record(result.id)
        assert record["target_kind"] == "workflow"

    def test_classify_launch_target_kind_ephemeral(self):
        """Unit test of the classifier create_terminal calls (full ephemeral
        launch requires registry binding well beyond this issue's scope;
        see ``test_ephemeral_service.py`` for that flow)."""
        from cli_agent_orchestrator.services.terminal_service import (
            _classify_launch_target_kind,
        )
        from cli_agent_orchestrator.utils import agent_profiles

        assert (
            _classify_launch_target_kind(agent_profiles.ProfileSource.EPHEMERAL, None)
            == "ephemeral"
        )

    def test_classify_launch_target_kind_workflow_beats_ephemeral(self):
        """Mutation guard: the workflow signal must be checked FIRST -- a
        workflow-step terminal using an ephemeral profile is still 'workflow'."""
        from cli_agent_orchestrator.services.terminal_service import (
            _classify_launch_target_kind,
        )
        from cli_agent_orchestrator.utils import agent_profiles

        assert (
            _classify_launch_target_kind(
                agent_profiles.ProfileSource.EPHEMERAL, {"CAO_WORKFLOW_RUN_ID": "run-1"}
            )
            == "workflow"
        )

    def test_classify_launch_target_kind_installed_default(self):
        from cli_agent_orchestrator.services.terminal_service import (
            _classify_launch_target_kind,
        )
        from cli_agent_orchestrator.utils import agent_profiles

        assert (
            _classify_launch_target_kind(agent_profiles.ProfileSource.INSTALLED, None)
            == "installed"
        )
        assert (
            _classify_launch_target_kind(agent_profiles.ProfileSource.INSTALLED, {}) == "installed"
        )

    @pytest.mark.asyncio
    @_patch_create_terminal_collaborators
    async def test_launch_record_write_failure_never_fails_create(
        self,
        mock_load_profile,
        mock_gen_id,
        mock_gen_session,
        mock_gen_window,
        mock_tmux,
        mock_db_create,
        mock_provider_manager,
        mock_fifo_dir,
        mock_fifo_manager,
        mock_status_monitor,
        mock_delete_terminals_by_session,
    ):
        """AC: 'Given that recording fails, then the launch result is unchanged.'"""
        _wire_common_mocks(
            mock_load_profile,
            mock_gen_id,
            mock_gen_session,
            mock_gen_window,
            mock_tmux,
            mock_db_create,
            mock_provider_manager,
            mock_fifo_dir,
        )

        with patch(
            "cli_agent_orchestrator.services.terminal_service.db_create_launch_record",
            side_effect=RuntimeError("db is down"),
        ):
            result = await create_terminal("kiro_cli", "developer", new_session=True)

        assert result.id == "test1234"
        assert result.status is not None  # create_terminal completed normally


class TestLaunchRecordOnTeardown:
    @pytest.mark.asyncio
    @_patch_create_terminal_collaborators
    async def test_delete_terminal_finalizes_launch_record(
        self,
        mock_load_profile,
        mock_gen_id,
        mock_gen_session,
        mock_gen_window,
        mock_tmux,
        mock_db_create,
        mock_provider_manager,
        mock_fifo_dir,
        mock_fifo_manager,
        mock_status_monitor,
        mock_delete_terminals_by_session,
    ):
        from cli_agent_orchestrator.clients import database

        _wire_common_mocks(
            mock_load_profile,
            mock_gen_id,
            mock_gen_session,
            mock_gen_window,
            mock_tmux,
            mock_db_create,
            mock_provider_manager,
            mock_fifo_dir,
        )
        result = await create_terminal("kiro_cli", "developer", new_session=True)

        # db_create_terminal is mocked above (as in TestCreateTerminal), so no
        # real TerminalModel row exists yet for delete_terminal to remove;
        # insert one with the same id to exercise the real teardown path.
        with database.SessionLocal() as db:
            db.add(
                database.TerminalModel(
                    id=result.id,
                    tmux_session=result.session_name,
                    tmux_window="developer-abcd",
                    provider="kiro_cli",
                )
            )
            db.commit()

        assert database.delete_terminal(result.id) is True

        record = database.get_launch_record(result.id)
        assert record is not None  # survives deletion of the terminal row
        assert record["ended_at"] is not None
        assert record["end_reason"] == "deleted"

    def test_finalize_failure_never_fails_delete_terminal(self):
        """AC: a recording failure must never fail or alter teardown.
        ``delete_terminal`` calls ``finalize_launch_records`` through its own
        ``_finalize_launch_records_best_effort`` wrapper (double-wrapped, same
        convention as ``_bind_decision_launch`` around ``bind_launch``) -- patch
        ``finalize_launch_records`` itself to raise, bypassing ITS internal
        try/except entirely, and confirm the outer wrapper still absorbs it so
        the terminal row delete reports success."""
        from cli_agent_orchestrator.clients import database

        with database.SessionLocal() as db:
            db.add(
                database.TerminalModel(
                    id="t-teardown",
                    tmux_session="s",
                    tmux_window="w",
                    provider="kiro_cli",
                )
            )
            db.commit()

        with patch.object(
            database, "finalize_launch_records", side_effect=RuntimeError("db is down")
        ):
            assert database.delete_terminal("t-teardown") is True

        with database.SessionLocal() as db:
            assert db.query(database.TerminalModel).filter_by(id="t-teardown").first() is None

    def test_finalize_best_effort_wrapper_absorbs_exception(self):
        """Direct unit test of the defense-in-depth wrapper itself."""
        from cli_agent_orchestrator.clients import database

        with patch.object(
            database, "finalize_launch_records", side_effect=RuntimeError("db is down")
        ):
            database._finalize_launch_records_best_effort(["x"], "deleted")  # must not raise


class TestClaudeCodeProviderSessionId:
    """Issue #1082: create_terminal generates and stores a fresh Claude Code
    session id, and never does so on a resumed launch."""

    @pytest.mark.asyncio
    @_patch_create_terminal_collaborators
    async def test_fresh_claude_code_launch_generates_session_id(
        self,
        mock_load_profile,
        mock_gen_id,
        mock_gen_session,
        mock_gen_window,
        mock_tmux,
        mock_db_create,
        mock_provider_manager,
        mock_fifo_dir,
        mock_fifo_manager,
        mock_status_monitor,
        mock_delete_terminals_by_session,
    ):
        import uuid as uuid_mod

        from cli_agent_orchestrator.clients import database

        _wire_common_mocks(
            mock_load_profile,
            mock_gen_id,
            mock_gen_session,
            mock_gen_window,
            mock_tmux,
            mock_db_create,
            mock_provider_manager,
            mock_fifo_dir,
        )

        result = await create_terminal("claude_code", "developer", new_session=True)

        session_id = mock_provider_manager.create_provider.call_args.kwargs["session_id"]
        assert session_id is not None
        uuid_mod.UUID(session_id)  # well-formed uuid
        assert mock_provider_manager.create_provider.call_args.kwargs["resume_session_id"] is None
        assert database.get_launch_record(result.id)["provider_session_id"] == session_id

    @pytest.mark.asyncio
    @_patch_create_terminal_collaborators
    async def test_resumed_claude_code_launch_never_generates_a_session_id(
        self,
        mock_load_profile,
        mock_gen_id,
        mock_gen_session,
        mock_gen_window,
        mock_tmux,
        mock_db_create,
        mock_provider_manager,
        mock_fifo_dir,
        mock_fifo_manager,
        mock_status_monitor,
        mock_delete_terminals_by_session,
    ):
        """AC: 'Given a resumed launch, then it still uses --resume, as today.'
        Mutation guard: session_id must stay None, not merely be ignored."""
        from cli_agent_orchestrator.clients import database

        _wire_common_mocks(
            mock_load_profile,
            mock_gen_id,
            mock_gen_session,
            mock_gen_window,
            mock_tmux,
            mock_db_create,
            mock_provider_manager,
            mock_fifo_dir,
        )

        result = await create_terminal(
            "claude_code",
            "developer",
            new_session=True,
            resume_session_id="11d55034-bb41-46ca-8686-59a9dbff16b5",
        )

        assert mock_provider_manager.create_provider.call_args.kwargs["session_id"] is None
        assert (
            mock_provider_manager.create_provider.call_args.kwargs["resume_session_id"]
            == "11d55034-bb41-46ca-8686-59a9dbff16b5"
        )
        assert (
            database.get_launch_record(result.id)["provider_session_id"]
            == "11d55034-bb41-46ca-8686-59a9dbff16b5"
        )

    @pytest.mark.asyncio
    @_patch_create_terminal_collaborators
    async def test_non_claude_code_provider_never_gets_a_session_id(
        self,
        mock_load_profile,
        mock_gen_id,
        mock_gen_session,
        mock_gen_window,
        mock_tmux,
        mock_db_create,
        mock_provider_manager,
        mock_fifo_dir,
        mock_fifo_manager,
        mock_status_monitor,
        mock_delete_terminals_by_session,
    ):
        """Mutation guard: the uuid generation is gated on provider == claude_code,
        not a bare 'resume_session_id is falsy' check."""
        from cli_agent_orchestrator.clients import database

        _wire_common_mocks(
            mock_load_profile,
            mock_gen_id,
            mock_gen_session,
            mock_gen_window,
            mock_tmux,
            mock_db_create,
            mock_provider_manager,
            mock_fifo_dir,
        )

        result = await create_terminal("kiro_cli", "developer", new_session=True)

        assert mock_provider_manager.create_provider.call_args.kwargs["session_id"] is None
        assert database.get_launch_record(result.id)["provider_session_id"] is None
