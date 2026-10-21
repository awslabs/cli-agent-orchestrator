"""A missing ``working_directory`` is created at the point of use, never earlier.

plauzy's #724 review reproduced three orphan directories: a 404 for a
nonexistent session, a 429 at the terminal cap and a 400 for a duplicate
``session_name`` all left the operator's new tree on disk, because the HTTP
boundary ``mkdir``'ed before ``terminal_service`` ran those checks. The mkdir
now lives inside ``create_terminal``'s locked section, after every one of them.
Same mocking seams as ``test_terminal_service_full.py``.
"""

from unittest.mock import patch

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.terminal import TerminalLimitError
from cli_agent_orchestrator.services.terminal_service import create_terminal
from cli_agent_orchestrator.utils.paths import WorkingDirectoryError

_TS = "cli_agent_orchestrator.services.terminal_service"


class _StopAtBackendCreate(Exception):
    """Raised by the mocked backend create so the test can prove the directory
    existed by then without standing up tmux."""


@pytest.fixture
def harness(tmp_path):
    with (
        patch(f"{_TS}.db_delete_terminal"),
        patch("cli_agent_orchestrator.backends.registry._backend") as backend,
        patch(f"{_TS}.generate_window_name", return_value="developer-abcd"),
        patch(f"{_TS}.generate_session_name", return_value="cao-session"),
        patch(f"{_TS}.generate_terminal_id", return_value="test1234"),
        patch(f"{_TS}.agent_profiles.load_agent_profile") as load_profile,
    ):
        load_profile.return_value = AgentProfile(name="developer", description="Developer")
        yield backend


@pytest.mark.asyncio
async def test_duplicate_session_name_creates_no_directory(harness, tmp_path):
    harness.session_exists.return_value = True
    target = tmp_path / "orphan" / "tree"
    with pytest.raises(ValueError, match="already exists"):
        await create_terminal(
            "kiro_cli",
            "developer",
            session_name="cao-dupe",
            new_session=True,
            working_directory=str(target),
            create_working_directory=True,
        )
    assert not target.exists(), "a duplicate-session rejection left the directory behind"
    harness.create_session.assert_not_called()


@pytest.mark.asyncio
async def test_session_not_found_creates_no_directory(harness, tmp_path):
    harness.session_exists.return_value = False
    target = tmp_path / "orphan" / "tree"
    with pytest.raises(ValueError, match="not found"):
        await create_terminal(
            "kiro_cli",
            "developer",
            session_name="cao-ghost",
            new_session=False,
            working_directory=str(target),
            create_working_directory=True,
        )
    assert not target.exists(), "a session-not-found rejection left the directory behind"
    harness.create_window.assert_not_called()


@pytest.mark.asyncio
async def test_terminal_cap_creates_no_directory(harness, tmp_path):
    target = tmp_path / "orphan" / "tree"
    with (
        patch(f"{_TS}.get_max_terminals", return_value=1),
        patch(f"{_TS}.count_runtime_allocated_terminals", return_value=1),
    ):
        with pytest.raises(TerminalLimitError):
            await create_terminal(
                "kiro_cli",
                "developer",
                session_name="cao-full",
                new_session=True,
                working_directory=str(target),
                create_working_directory=True,
            )
    assert not target.exists(), "a terminal-cap rejection left the directory behind"


@pytest.mark.asyncio
async def test_blocked_path_is_refused_before_the_lock_and_never_created(harness):
    """The policy check (#345 D5) runs where the backend's own check used to,
    so ``/tmp/<new>`` is refused by its nearest existing ancestor instead of
    being created and then waved through."""
    import uuid
    from pathlib import Path

    target = Path("/tmp") / f"cao-724-{uuid.uuid4().hex}"
    harness.session_exists.return_value = False
    try:
        with pytest.raises(WorkingDirectoryError, match="nearest existing ancestor"):
            await create_terminal(
                "kiro_cli",
                "developer",
                session_name="cao-blocked",
                new_session=True,
                working_directory=str(target),
                create_working_directory=True,
            )
        assert not target.exists()
        harness.session_exists.assert_not_called()
    finally:
        if target.exists():  # pragma: no cover - only on a failed assertion
            target.rmdir()


@pytest.mark.asyncio
@pytest.mark.parametrize("new_session", [True, False])
async def test_directory_exists_by_the_time_the_backend_is_asked_to_create(
    harness, tmp_path, new_session
):
    """The happy half of the contract: once every check has passed, the
    directory is there for tmux to ``cd`` into."""
    target = tmp_path / "new" / "project"
    harness.session_exists.return_value = not new_session
    seen = {}

    def backend_create(*args, **kwargs):
        seen["exists"] = target.is_dir()
        raise _StopAtBackendCreate

    harness.create_session.side_effect = backend_create
    harness.create_window.side_effect = backend_create
    with pytest.raises(_StopAtBackendCreate):
        await create_terminal(
            "kiro_cli",
            "developer",
            session_name="cao-happy",
            new_session=new_session,
            working_directory=str(target),
            create_working_directory=True,
        )
    assert seen == {"exists": True}


@pytest.mark.asyncio
async def test_without_the_flag_a_missing_directory_is_still_refused(harness, tmp_path):
    """The MCP ``handoff``/``assign`` path never asked for creation and must
    not start getting it: the default keeps the backend's must-exist rule."""
    target = tmp_path / "never"
    harness.session_exists.return_value = False
    with pytest.raises(ValueError, match="does not exist"):
        await create_terminal(
            "kiro_cli",
            "developer",
            session_name="cao-mcp",
            new_session=True,
            working_directory=str(target),
        )
    assert not target.exists()
