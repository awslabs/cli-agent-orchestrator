"""SC-6 — ``memory.enabled`` settings-flag tests (U5).

Covers the master enable/disable switch introduced in U5:

- **AC1** — ``is_memory_enabled()`` reflects the ``memory.enabled`` key in
  ``settings.json`` and defaults to True (opt-out) when absent.
- **AC2** — Every public entry point on ``MemoryService`` short-circuits when
  disabled:
    * ``store()``    → raises ``MemoryDisabledError``
    * ``recall()``   → returns ``[]``
    * ``forget()``   → raises ``MemoryDisabledError``
    * ``get_memory_context_for_terminal()``   → returns ``""``
    * ``get_curated_memory_context()``        → returns ``""``
- **AC3** — Disabled ``store()`` touches neither the filesystem (no wiki
  files, no ``index.md``) nor SQLite (no metadata row).
- **AC4** — With ``enabled=True`` (default) behavior is unchanged — a
  round-trip store + recall still works.
- **MCP** — The MCP memory tools return an explicit ``disabled`` discriminator
  plus the shared ``MEMORY_DISABLED_MESSAGE`` so the agent can surface a
  clear error to the user.

Together these meet SC-6 in ``aidlc-docs/phase2.5/success-criteria.md``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine

from cli_agent_orchestrator.clients.database import Base, MemoryMetadataModel
from cli_agent_orchestrator.services.memory_service import (
    MemoryDisabledError,
    MemoryService,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(terminal_id: str = "term-u5") -> dict:
    return {
        "terminal_id": terminal_id,
        "session_name": "sess-u5",
        "agent_profile": "dev",
        "provider": "claude_code",
        "cwd": "/home/user/proj-u5",
    }


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _make_engine(db_path: Path) -> Any:
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    return engine


def _make_svc(base_dir: Path, db_path: Path) -> MemoryService:
    engine = _make_engine(db_path)
    svc = MemoryService(base_dir=base_dir, db_engine=engine)
    svc._get_terminal_context = lambda terminal_id: _ctx()  # type: ignore[method-assign]
    return svc


@pytest.fixture
def settings_file(tmp_path: Path) -> Any:
    """Patch settings_service paths to an isolated settings.json."""
    fake_settings = tmp_path / "settings.json"
    with (
        patch(
            "cli_agent_orchestrator.services.settings_service.SETTINGS_FILE",
            fake_settings,
        ),
        patch(
            "cli_agent_orchestrator.services.settings_service.CAO_HOME_DIR",
            tmp_path,
        ),
    ):
        yield fake_settings


# ---------------------------------------------------------------------------
# AC1 — is_memory_enabled() reflects the settings flag
# ---------------------------------------------------------------------------


class TestIsMemoryEnabledFlag:
    def test_defaults_to_true_when_absent(self, settings_file: Path) -> None:
        """With no ``memory`` key in settings.json, default is True (opt-out)."""
        from cli_agent_orchestrator.services.settings_service import is_memory_enabled

        assert not settings_file.exists()
        assert is_memory_enabled() is True

    def test_returns_false_when_explicitly_disabled(self, settings_file: Path) -> None:
        """``memory.enabled`` false disables the subsystem."""
        from cli_agent_orchestrator.services.settings_service import is_memory_enabled

        settings_file.write_text(json.dumps({"memory": {"enabled": False}}))
        assert is_memory_enabled() is False

    def test_returns_true_when_explicitly_enabled(self, settings_file: Path) -> None:
        from cli_agent_orchestrator.services.settings_service import is_memory_enabled

        settings_file.write_text(json.dumps({"memory": {"enabled": True}}))
        assert is_memory_enabled() is True

    def test_set_memory_setting_enabled_roundtrip(self, settings_file: Path) -> None:
        """``set_memory_setting('enabled', False)`` persists and round-trips."""
        from cli_agent_orchestrator.services.settings_service import (
            get_memory_settings,
            is_memory_enabled,
            set_memory_setting,
        )

        set_memory_setting("enabled", False)
        assert is_memory_enabled() is False
        # Flush to disk AND merge with the other default preserved.
        settings = get_memory_settings()
        assert settings["enabled"] is False
        assert settings["flush_threshold"] == 0.85

        set_memory_setting("enabled", True)
        assert is_memory_enabled() is True

    def test_set_memory_setting_rejects_non_bool(self, settings_file: Path) -> None:
        from cli_agent_orchestrator.services.settings_service import set_memory_setting

        with pytest.raises(ValueError, match="enabled must be a bool"):
            set_memory_setting("enabled", "yes")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AC2 — every MemoryService entry point short-circuits when disabled
# ---------------------------------------------------------------------------


class TestMemoryServiceShortCircuits:
    """When the flag is off, no entry point should touch FS or SQLite."""

    def _disable(self) -> Any:
        return patch(
            "cli_agent_orchestrator.services.memory_service._is_memory_enabled",
            return_value=False,
        )

    def test_store_raises_memory_disabled_error(self, tmp_path: Path) -> None:
        svc = _make_svc(tmp_path / "mem", tmp_path / "u5.db")
        with self._disable():
            with pytest.raises(MemoryDisabledError):
                _run(
                    svc.store(
                        content="should not be stored",
                        scope="global",
                        memory_type="project",
                        key="never-land",
                        terminal_context=_ctx(),
                    )
                )

    def test_recall_returns_empty_list(self, tmp_path: Path) -> None:
        svc = _make_svc(tmp_path / "mem", tmp_path / "u5.db")
        with self._disable():
            out = _run(
                svc.recall(
                    scope="global",
                    limit=50,
                    search_mode="metadata",
                    terminal_context=_ctx(),
                )
            )
        assert out == []

    def test_forget_raises_memory_disabled_error(self, tmp_path: Path) -> None:
        svc = _make_svc(tmp_path / "mem", tmp_path / "u5.db")
        with self._disable():
            with pytest.raises(MemoryDisabledError):
                _run(
                    svc.forget(
                        key="nope",
                        scope="global",
                        terminal_context=_ctx(),
                    )
                )

    def test_get_memory_context_for_terminal_returns_empty_string(
        self, tmp_path: Path
    ) -> None:
        svc = _make_svc(tmp_path / "mem", tmp_path / "u5.db")
        with self._disable():
            assert svc.get_memory_context_for_terminal("term-u5") == ""

    def test_get_curated_memory_context_returns_empty_string(
        self, tmp_path: Path
    ) -> None:
        svc = _make_svc(tmp_path / "mem", tmp_path / "u5.db")
        with self._disable():
            assert svc.get_curated_memory_context("term-u5") == ""


# ---------------------------------------------------------------------------
# AC3 — disabled store writes nothing to filesystem or SQLite
# ---------------------------------------------------------------------------


def test_disabled_store_writes_nothing_to_filesystem_or_sqlite(tmp_path: Path) -> None:
    """When disabled, ``store()`` must not create a wiki file, index.md, or
    SQLite row — the guard is the FIRST line of ``store()``.

    AC3: "Tests: flag-off means no FS/SQLite writes." (tasks.md U5.)
    """
    base_dir = tmp_path / "mem"
    base_dir.mkdir()
    db_path = tmp_path / "u5-noio.db"
    svc = _make_svc(base_dir, db_path)

    # Snapshot the empty state.
    before_fs = sorted(p.relative_to(base_dir) for p in base_dir.rglob("*"))
    assert before_fs == [], f"base_dir should start empty, got {before_fs}"

    with patch(
        "cli_agent_orchestrator.services.memory_service._is_memory_enabled",
        return_value=False,
    ):
        with pytest.raises(MemoryDisabledError):
            _run(
                svc.store(
                    content="body that must never hit disk",
                    scope="global",
                    memory_type="project",
                    key="ghost-key",
                    tags="ghost",
                    terminal_context=_ctx(),
                )
            )

    # Filesystem invariant — nothing was created.
    after_fs = sorted(p.relative_to(base_dir) for p in base_dir.rglob("*"))
    assert after_fs == [], (
        f"disabled store() must not create any files under base_dir; got {after_fs}"
    )
    # Per-scope index.md must also not exist.
    assert not svc.get_index_path("global", None).exists(), (
        "disabled store() must not materialize index.md"
    )

    # SQLite invariant — no metadata row was inserted.
    with svc._get_db_session() as db:
        rows = (
            db.query(MemoryMetadataModel)
            .filter_by(key="ghost-key", scope="global")
            .all()
        )
        assert rows == [], (
            f"disabled store() must not insert SQLite metadata; got {rows}"
        )


# ---------------------------------------------------------------------------
# AC4 — enabled=True (default) preserves existing behavior
# ---------------------------------------------------------------------------


def test_enabled_default_preserves_round_trip(tmp_path: Path) -> None:
    """With the flag on (default), store → recall still works end-to-end.

    Forces the guard to return True and confirms an unchanged behavior path.
    """
    svc = _make_svc(tmp_path / "mem", tmp_path / "u5-on.db")
    with patch(
        "cli_agent_orchestrator.services.memory_service._is_memory_enabled",
        return_value=True,
    ):
        memory = _run(
            svc.store(
                content="round-trip body",
                scope="global",
                memory_type="project",
                key="rt-01",
                tags="rt",
                terminal_context=_ctx(),
            )
        )
        assert memory.key == "rt-01"
        assert memory.scope == "global"

        recalled = _run(
            svc.recall(
                scope="global",
                limit=10,
                search_mode="metadata",
                terminal_context=_ctx(),
            )
        )

    recalled_keys = {m.key for m in recalled}
    assert "rt-01" in recalled_keys, (
        f"enabled-path round-trip failed: rt-01 not recalled from {recalled_keys}"
    )

    # Injection context block must also populate when enabled.
    block = svc.get_memory_context_for_terminal("term-u5", budget_chars=10_000)
    assert "rt-01" in block


# ---------------------------------------------------------------------------
# AC5 — guard order: short-circuit runs BEFORE any validation
# ---------------------------------------------------------------------------


def test_disabled_store_short_circuits_before_validation(tmp_path: Path) -> None:
    """The disabled guard must fire before scope/type validation, so a
    disabled-memory state returns the canonical ``MemoryDisabledError`` even
    when inputs would otherwise raise ``ValueError``.

    Regression guard — if an eager ``MemoryScope(scope)`` is ever moved above
    the enable guard, callers would see a misleading error for a legitimate
    "memory disabled" state.
    """
    svc = _make_svc(tmp_path / "mem", tmp_path / "u5-guard.db")
    with patch(
        "cli_agent_orchestrator.services.memory_service._is_memory_enabled",
        return_value=False,
    ):
        with pytest.raises(MemoryDisabledError):
            _run(
                svc.store(
                    content="x",
                    scope="NOT-A-SCOPE",  # would raise ValueError if reached
                    memory_type="project",
                    key="x",
                    terminal_context=_ctx(),
                )
            )


# ---------------------------------------------------------------------------
# MCP tool surface — disabled responses carry ``disabled: True`` + message
# ---------------------------------------------------------------------------


class TestMcpToolsDisabledSurface:
    """MCP tools must return ``{success: False, disabled: True, error: ...}``
    when the subsystem is off. The agent surfaces ``error`` to the user so
    the state is obvious, not a generic failure.
    """

    def test_memory_disabled_message_is_actionable(self) -> None:
        from cli_agent_orchestrator.mcp_server.server import MEMORY_DISABLED_MESSAGE

        # Must name the flag + config file so the user can self-fix.
        assert "memory.enabled" in MEMORY_DISABLED_MESSAGE
        assert "settings.json" in MEMORY_DISABLED_MESSAGE

    def test_memory_store_returns_disabled_payload(self, tmp_path: Path) -> None:
        """``memory_store`` tool surfaces MemoryDisabledError as a structured
        response (never an unhandled exception).
        """
        from cli_agent_orchestrator.mcp_server import server as srv
        from cli_agent_orchestrator.mcp_server.server import memory_store

        with (
            patch(
                "cli_agent_orchestrator.services.memory_service._is_memory_enabled",
                return_value=False,
            ),
            patch.object(srv, "_get_terminal_context_from_env", return_value=_ctx()),
            patch(
                "cli_agent_orchestrator.services.memory_service.MemoryService",
                lambda: _make_svc(tmp_path / "mem", tmp_path / "u5-mcp.db"),
            ),
        ):
            result = _run(
                memory_store(
                    content="x",
                    scope="global",
                    memory_type="project",
                    key="k",
                    tags="",
                )
            )

        assert result["success"] is False
        assert result["disabled"] is True
        assert result["error"] == srv.MEMORY_DISABLED_MESSAGE

    def test_memory_recall_returns_disabled_payload(self, tmp_path: Path) -> None:
        """``memory_recall`` pre-checks the flag before calling the service,
        so disabled state is observable without entering the service.
        """
        from cli_agent_orchestrator.mcp_server import server as srv
        from cli_agent_orchestrator.mcp_server.server import memory_recall

        with patch(
            "cli_agent_orchestrator.services.settings_service.is_memory_enabled",
            return_value=False,
        ):
            result = _run(
                memory_recall(
                    query=None,
                    scope=None,
                    memory_type=None,
                    limit=10,
                    search_mode="hybrid",
                )
            )

        assert result["success"] is False
        assert result["disabled"] is True
        assert result["error"] == srv.MEMORY_DISABLED_MESSAGE
        # Caller contract: a disabled recall still returns an empty memories list.
        assert result["memories"] == []

    def test_memory_forget_returns_disabled_payload(self, tmp_path: Path) -> None:
        from cli_agent_orchestrator.mcp_server import server as srv
        from cli_agent_orchestrator.mcp_server.server import memory_forget

        with (
            patch(
                "cli_agent_orchestrator.services.memory_service._is_memory_enabled",
                return_value=False,
            ),
            patch.object(srv, "_get_terminal_context_from_env", return_value=_ctx()),
            patch(
                "cli_agent_orchestrator.services.memory_service.MemoryService",
                lambda: _make_svc(tmp_path / "mem", tmp_path / "u5-forget.db"),
            ),
        ):
            result = _run(
                memory_forget(
                    key="k",
                    scope="global",
                )
            )

        assert result["success"] is False
        assert result["disabled"] is True
        assert result["error"] == srv.MEMORY_DISABLED_MESSAGE

    def test_memory_consolidate_returns_disabled_payload(self, tmp_path: Path) -> None:
        """``memory_consolidate`` surfaces the disabled state through the
        same structured shape — the MemoryDisabledError is raised inside
        ``store()`` at the top of the consolidation sequence.
        """
        from cli_agent_orchestrator.mcp_server import server as srv
        from cli_agent_orchestrator.mcp_server.server import memory_consolidate

        with (
            patch(
                "cli_agent_orchestrator.services.memory_service._is_memory_enabled",
                return_value=False,
            ),
            patch.object(srv, "_get_terminal_context_from_env", return_value=_ctx()),
            patch(
                "cli_agent_orchestrator.services.memory_service.MemoryService",
                lambda: _make_svc(tmp_path / "mem", tmp_path / "u5-con.db"),
            ),
        ):
            result = _run(
                memory_consolidate(
                    keys=["a", "b"],
                    new_content="merged",
                    scope="global",
                    new_key=None,
                    memory_type="project",
                    tags="",
                )
            )

        assert result["success"] is False
        assert result["disabled"] is True
        assert result["error"] == srv.MEMORY_DISABLED_MESSAGE
