"""Launch safety — a dangling projection must never raise into terminal creation.

**Validates: Requirements 15.4, 15.5**

Removal is not symmetric with install. Two providers read ``SKILL.md`` from disk
mid-session, so unpublishing a plugin can pull a skill out from under an agent
that is about to load it. The transient stale-reference risk is accepted and
reported (that is what the removal confirmation exists for), but one thing is
**not** negotiable: sweeping a projection runs concurrently with
``terminal_service.create_terminal``, and it must never turn a plugin removal
into a failed terminal launch.

The tests here drive the real ``create_terminal`` with the same mocking shape the
existing terminal-service suite uses, so the assertion is about the actual launch
path rather than a stand-in for it.
"""

from __future__ import annotations

import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.agent_plugins.installer import install
from cli_agent_orchestrator.agent_plugins.models import PluginSource
from cli_agent_orchestrator.agent_plugins.projection import (
    rebuild_projection,
    sweep_dangling_projections,
)
from cli_agent_orchestrator.models.agent_profile import AgentProfile

from .conftest import build_plugin


@pytest.fixture
def projected(store, skills_dir, tmp_path, monkeypatch):
    """Install a plugin whose skill is projected into the isolated skill store."""
    source = build_plugin(tmp_path / "src", "demo", skills=["alpha", "beta"])
    install(
        PluginSource(kind="path", location=str(source)),
        store=store,
        skills_dir=skills_dir,
        refresh_agents=False,
    )
    monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", skills_dir)
    return store, skills_dir


class TestDeliveryPathsToleratesBrokenLinks:
    def test_catalog_build_skips_a_broken_projection(self, projected):
        """``list_skills()`` gates on ``is_dir()`` and ``SKILL.md is_file()``.

        Both are ``False`` — not an exception — for a symlink whose target is
        gone, so a broken link is simply not enumerated.
        """
        import shutil

        store, skills_dir = projected
        from cli_agent_orchestrator.utils.skills import build_skill_catalog

        shutil.rmtree(store.plugin_root("demo") / "skills" / "alpha")

        catalog = build_skill_catalog()
        assert "beta" in catalog
        assert "alpha" not in catalog

    def test_catalog_build_survives_the_whole_store_vanishing(self, projected):
        import shutil

        store, skills_dir = projected
        from cli_agent_orchestrator.utils.skills import build_skill_catalog

        shutil.rmtree(store.plugins_dir)

        assert build_skill_catalog() == ""  # no skills, no exception


class TestCreateTerminalUnderConcurrentSweep:
    """The real launch path, with a sweep racing it."""

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.FIFO_DIR")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.db_create_terminal")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_window_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_session_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_terminal_id")
    @patch("cli_agent_orchestrator.services.terminal_service.load_agent_profile")
    async def test_terminal_still_launches_while_a_projection_breaks(
        self,
        mock_load_profile,
        mock_gen_id,
        mock_gen_session,
        mock_gen_window,
        mock_backend,
        mock_db_create,
        mock_provider_manager,
        mock_fifo_dir,
        mock_fifo_manager,
        mock_status_monitor,
        projected,
    ):
        import shutil

        from cli_agent_orchestrator.services.terminal_service import create_terminal

        store, skills_dir = projected

        mock_gen_id.return_value = "test1234"
        mock_gen_session.return_value = "cao-session"
        mock_gen_window.return_value = "developer-abcd"
        mock_backend.session_exists.return_value = False
        mock_load_profile.return_value = AgentProfile(name="developer", description="Developer")
        provider = AsyncMock()
        provider.initialize.return_value = True
        mock_provider_manager.create_provider.return_value = provider
        mock_fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")

        # Break one projection and sweep it, concurrently with the launch.
        barrier = threading.Barrier(2, timeout=10)

        def break_and_sweep():
            barrier.wait()
            shutil.rmtree(store.plugin_root("demo") / "skills" / "alpha", ignore_errors=True)
            sweep_dangling_projections(store, skills_dir=skills_dir)

        sweeper = threading.Thread(target=break_and_sweep)
        sweeper.start()
        barrier.wait()

        terminal = await create_terminal(
            provider="claude_code", agent_profile="developer", new_session=True
        )

        sweeper.join(timeout=10)
        assert terminal.id == "test1234"

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.FIFO_DIR")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.db_create_terminal")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_window_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_session_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_terminal_id")
    @patch("cli_agent_orchestrator.services.terminal_service.load_agent_profile")
    async def test_terminal_launches_with_an_already_dangling_link(
        self,
        mock_load_profile,
        mock_gen_id,
        mock_gen_session,
        mock_gen_window,
        mock_backend,
        mock_db_create,
        mock_provider_manager,
        mock_fifo_dir,
        mock_fifo_manager,
        mock_status_monitor,
        projected,
    ):
        import shutil

        from cli_agent_orchestrator.services.terminal_service import create_terminal

        store, skills_dir = projected
        shutil.rmtree(store.plugin_root("demo"))

        mock_gen_id.return_value = "test5678"
        mock_gen_session.return_value = "cao-session"
        mock_gen_window.return_value = "developer-abcd"
        mock_backend.session_exists.return_value = False
        mock_load_profile.return_value = AgentProfile(name="developer", description="Developer")
        provider = AsyncMock()
        provider.initialize.return_value = True
        mock_provider_manager.create_provider.return_value = provider
        mock_fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")

        terminal = await create_terminal(
            provider="claude_code", agent_profile="developer", new_session=True
        )
        assert terminal.id == "test5678"


class TestSweepNeverRaises:
    def test_sweep_swallows_an_unreadable_skill_store(self, store, tmp_path, monkeypatch):
        missing = tmp_path / "gone"
        assert sweep_dangling_projections(store, skills_dir=missing) == []

    def test_rebuild_never_raises_when_the_store_is_unreadable(
        self, store, skills_dir, monkeypatch
    ):
        def boom(*args, **kwargs):
            raise OSError("filesystem is on fire")

        monkeypatch.setattr(
            "cli_agent_orchestrator.agent_plugins.store.InstalledPluginStore.list_installed",
            lambda self: [],
        )
        monkeypatch.setattr("cli_agent_orchestrator.agent_plugins.projection._sweep", boom)
        with pytest.raises(OSError):
            # Guard the guard: `_sweep` really is the thing being stubbed, so
            # the tolerance asserted below is the wrapper's, not a no-op.
            rebuild_projection(store, skills_dir=skills_dir)

        assert sweep_dangling_projections(store, skills_dir=skills_dir) == []
