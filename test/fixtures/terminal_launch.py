"""Isolate provider startup while exercising the real terminal launch function."""

from unittest.mock import AsyncMock, MagicMock, Mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.services import install_service, terminal_service
from cli_agent_orchestrator.utils import agent_profiles


def install_launch_stubs(monkeypatch, root, *, profile_model="model-y"):
    from cli_agent_orchestrator.clients import database

    database_engine = create_engine(f"sqlite:///{root / 'terminal-state.db'}")
    database.Base.metadata.create_all(bind=database_engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=database_engine))
    profile = AgentProfile(name="developer", description="Developer", model=profile_model)
    backend = MagicMock()
    backend.session_exists.return_value = False
    backend.session_exists_strict.return_value = False
    backend.create_session.return_value = "developer-worker"
    backend.create_window.return_value = "developer-worker"
    provider = AsyncMock()
    provider.shell_baseline = None
    provider.runtime_variant = None
    # A healthy launch: a Kiro provider reports its passed identity check (#1074).
    provider.identity_verified = True
    providers = MagicMock()
    providers.create_provider.return_value = provider
    for name, value in {
        "get_backend": Mock(return_value=backend),
        "generate_terminal_id": Mock(return_value="worker-fixed"),
        "generate_window_name": Mock(return_value="developer-worker"),
        "generate_session_name": Mock(return_value="cao-fixture"),
        "get_max_terminals": Mock(return_value=None),
        "db_create_terminal": Mock(),
        # The row is mocked, so the identity record has none to be written to.
        "mark_terminal_identity_verified": Mock(return_value=True),
        "db_delete_terminal": Mock(),
        "delete_terminals_by_session": Mock(),
        "fifo_manager": MagicMock(),
        "status_monitor": MagicMock(),
        "provider_manager": providers,
        "get_herdr_inbox_service": Mock(return_value=None),
        "FIFO_DIR": root,
        "build_skill_catalog": Mock(return_value=""),
        "_resolve_working_directory": lambda directory: directory or str(root),
        "_confirm_worker_started_or_resubmit": AsyncMock(return_value=True),
    }.items():
        monkeypatch.setattr(terminal_service, name, value)
    # The INSTALLED profile is installed for Kiro as well: a kiro_cli launch refuses
    # an agent no agent directory defines (#1074).
    kiro_agents = root / "kiro-agents"
    kiro_agents.mkdir(exist_ok=True)
    (kiro_agents / "developer.json").write_text('{"name": "developer"}', encoding="utf-8")
    monkeypatch.setattr(install_service, "KIRO_AGENTS_DIR", kiro_agents)
    # The launch path loads profiles through the store router, which also reports the source.
    monkeypatch.setattr(
        agent_profiles,
        "load_launch_profile",
        Mock(return_value=(profile, agent_profiles.ProfileSource.INSTALLED)),
    )
    return provider, backend, providers
