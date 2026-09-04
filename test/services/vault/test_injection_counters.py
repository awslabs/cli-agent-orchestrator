"""Durable accounting for vault injection clipping."""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base, VaultRecallCounterModel
from cli_agent_orchestrator.constants import MEMORY_SCOPE_BUDGET_CHARS
from cli_agent_orchestrator.models.memory import Memory
from cli_agent_orchestrator.services.memory_service import MemoryService
from cli_agent_orchestrator.services.vault.binding import VaultBinding
from cli_agent_orchestrator.services.vault.config import FolderMapping


def _vault_memory(key: str, *, source_kind: str = "vault") -> Memory:
    return Memory(
        id=key,
        key=key,
        memory_type="reference",
        scope="global",
        scope_id=None,
        file_path="/internal",
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        content="body",
        source_kind=source_kind,
    )


def test_vault_clip_records_scope_and_exact_dropped_memory_magnitudes(monkeypatch) -> None:
    """A clip writes the two content-free counters, not a process-local global."""
    from cli_agent_orchestrator.services import memory_service, settings_service
    from cli_agent_orchestrator.services.vault import binding

    vault_binding = VaultBinding(
        scope="global",
        scope_id=None,
        vault_id="fixture",
        root="/vault",
        mapping=FolderMapping(folder="CAO", scope="global"),
    )
    calls: list[tuple[str, str, int]] = []
    monkeypatch.setattr(settings_service, "get_vault_config", lambda: object())
    monkeypatch.setattr(binding, "resolve", lambda *_args, **_kwargs: vault_binding)
    monkeypatch.setattr(
        memory_service,
        "increment_counter",
        lambda vault_id, counter_name, amount: calls.append((vault_id, counter_name, amount)),
    )

    MemoryService()._record_vault_injection_clip(
        "global", None, [_vault_memory("first"), _vault_memory("second")]
    )

    assert calls == [
        ("fixture", "injection_budget_exceeded.scopes_clipped", 1),
        ("fixture", "injection_budget_exceeded.memories_dropped", 2),
    ]


def test_native_clip_performs_no_vault_counter_write(monkeypatch) -> None:
    """A clean native injection and native clip never mutate vault counters."""
    from cli_agent_orchestrator.services import memory_service, settings_service
    from cli_agent_orchestrator.services.vault import binding

    calls: list[tuple[str, str, int]] = []
    config_reads: list[object] = []
    vault_binding = VaultBinding(
        scope="global",
        scope_id=None,
        vault_id="fixture",
        root="/vault",
        mapping=FolderMapping(folder="CAO", scope="global"),
    )
    monkeypatch.setattr(
        settings_service, "get_vault_config", lambda: config_reads.append(object()) or object()
    )
    monkeypatch.setattr(binding, "resolve", lambda *_args, **_kwargs: vault_binding)
    monkeypatch.setattr(
        memory_service,
        "increment_counter",
        lambda vault_id, counter_name, amount: calls.append((vault_id, counter_name, amount)),
    )

    MemoryService()._record_vault_injection_clip(
        "global", None, [_vault_memory("native", source_kind="native")]
    )
    assert config_reads == []
    assert calls == []


def test_related_vault_budget_skip_records_a_distinct_counter(monkeypatch) -> None:
    """A related skip is visible without inflating the primary-drop magnitude."""
    from cli_agent_orchestrator.services import memory_service, settings_service
    from cli_agent_orchestrator.services.vault import binding

    vault_binding = VaultBinding(
        scope="global",
        scope_id=None,
        vault_id="fixture",
        root="/vault",
        mapping=FolderMapping(folder="CAO", scope="global"),
    )
    calls: list[tuple[str, str, int]] = []
    monkeypatch.setattr(settings_service, "get_vault_config", lambda: object())
    monkeypatch.setattr(binding, "resolve", lambda *_args, **_kwargs: vault_binding)
    monkeypatch.setattr(
        memory_service,
        "increment_counter",
        lambda vault_id, counter_name, amount: calls.append((vault_id, counter_name, amount)),
    )

    MemoryService()._record_vault_related_injection_skip(_vault_memory("related"))

    assert calls == [
        ("fixture", "injection_budget_exceeded.related_memories_dropped", 1),
    ]


def test_empty_dropped_list_performs_no_counter_write(monkeypatch) -> None:
    """The helper reaches neither config nor a committing counter session."""
    from cli_agent_orchestrator.services import memory_service, settings_service

    calls: list[tuple[str, str, int]] = []
    config_reads: list[object] = []
    monkeypatch.setattr(
        memory_service,
        "increment_counter",
        lambda vault_id, counter_name, amount: calls.append((vault_id, counter_name, amount)),
    )
    monkeypatch.setattr(
        settings_service,
        "get_vault_config",
        lambda: config_reads.append(object()) or object(),
    )
    MemoryService()._record_vault_injection_clip("global", None, [])
    assert config_reads == []
    assert calls == []


def test_zero_counter_amount_writes_no_row(tmp_path, monkeypatch) -> None:
    """A future computed counter amount of zero remains a write-free no-op."""
    from cli_agent_orchestrator.services.vault import reader

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(reader, "SessionLocal", Session)

    reader.increment_counter("fixture", "counter", 0)

    with Session() as db:
        assert db.query(VaultRecallCounterModel).count() == 0


def test_status_reads_recall_counters_written_in_another_process(tmp_path) -> None:
    """Status reads the durable table, not a server-process module global."""
    repository_root = Path(__file__).parents[3]
    home = tmp_path / "cao-home"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    (vault_root / "CAO").mkdir()
    env = {
        **os.environ,
        "CAO_HOME_DIR": str(home),
        "PYTHONPATH": str(repository_root / "src"),
    }
    config_source = (
        "from cli_agent_orchestrator.services.vault.config import "
        "FolderMapping, VaultConfig, VaultSpec; "
        f"config = VaultConfig(enabled=True, vaults=[VaultSpec(id='fixture', root={str(vault_root)!r}, "
        "managed_folder='CAO', mappings=[FolderMapping(folder='CAO', scope='global', writable=True)])]); "
    )
    writer = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from cli_agent_orchestrator.clients.database import init_db; "
                "from cli_agent_orchestrator.services.vault.reader import increment_counter; "
                "init_db(); "
                "increment_counter('fixture', 'injection_budget_exceeded.scopes_clipped', 1); "
                "increment_counter('fixture', 'injection_budget_exceeded.memories_dropped', 3)"
            ),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert writer.returncode == 0, writer.stderr
    reader = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; "
                + config_source
                + "from cli_agent_orchestrator.services.vault.status import get_vault_status; "
                + "print(json.dumps(dict(get_vault_status(config)[0].recall_counters), sort_keys=True))"
            ),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert reader.returncode == 0, reader.stderr
    assert json.loads(reader.stdout) == {
        "injection_budget_exceeded.memories_dropped": 3,
        "injection_budget_exceeded.scopes_clipped": 1,
    }


def test_synthetic_clip_hits_the_shipped_scope_budget() -> None:
    """Default injection arithmetic caps a 4096-char served body at 1000 chars."""
    scope_char_cap = min(MEMORY_SCOPE_BUDGET_CHARS, 3000 // 3)
    body_cap = 4096
    memory = _vault_memory("long")
    memory.content = "x" * body_cap
    line = f"- [{memory.scope}] {memory.key}: {memory.content}"

    assert scope_char_cap == MEMORY_SCOPE_BUDGET_CHARS
    assert len(line) > scope_char_cap
