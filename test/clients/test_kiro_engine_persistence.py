"""Kiro engine persistence and legacy restoration coverage."""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as db_mod


def test_engine_round_trip_and_legacy_kiro_default(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'terminals.db'}")
    db_mod.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", sessionmaker(bind=engine))

    created = db_mod.create_terminal(
        "newkiro1",
        "cao-session",
        "window",
        "kiro_cli",
        "developer",
        engine="kas",
    )
    assert created["engine"] == "kas"
    assert db_mod.get_terminal_metadata("newkiro1")["engine"] == "kas"

    db_mod.create_terminal("legacy01", "cao-session", "old", "kiro_cli", "developer")
    assert db_mod.get_terminal_metadata("legacy01")["engine"] == "v2"


def test_non_kiro_terminal_has_no_engine(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'terminals.db'}")
    db_mod.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", sessionmaker(bind=engine))

    db_mod.create_terminal("other001", "cao-session", "window", "codex", "developer")
    assert db_mod.get_terminal_metadata("other001")["engine"] is None
