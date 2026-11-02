"""Probes run under a stand-in CAO home by ``test_lifespan_ephemeral_sweep`` (#1079).

The file name keeps this module out of normal collection. The outer test seeds the
stand-in home's ephemeral store and database, runs these tests in a subprocess with the
repo's conftest, then checks that the store, its rows and its lock dir are unchanged.
"""

from test.api.test_lifespan_inbox import _patched_lifespan
from test.services.test_ephemeral_service import create, create_store  # noqa: F401
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.api.main import app, lifespan
from cli_agent_orchestrator.clients import database


@pytest.mark.asyncio
async def test_lifespan_with_a_replacement_database(tmp_path, monkeypatch):
    """The home's live files have no rows in this database, so they look rowless."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'replacement.db'}", connect_args={"check_same_thread": False}
    )
    database.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
    try:
        with _patched_lifespan(MagicMock(), []):
            async with lifespan(app):
                pass
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_lifespan_with_the_home_database():
    """The home's aged gc record would be pruned if its archive were in reach."""
    with _patched_lifespan(MagicMock(), []):
        async with lifespan(app):
            pass


def test_a_create_through_a_test_store(create_store):
    """The create's store lock file belongs in the test's lock dir."""
    assert create(create_store)["name"]
