"""``memory.learning_enabled`` settings-flag tests (self-learning Phase 1).

Covers the opt-in switch for workflow self-learning (outcome capture):

- **AC1** — ``is_learning_enabled()`` reflects ``memory.learning_enabled`` in
  ``settings.json`` and defaults to False (opt-in) when absent.
- **AC2** — Learning is a child of the memory subsystem: ``memory.enabled``
  False forces learning off regardless of ``learning_enabled``.
- **AC3** — ``CAO_MEMORY_LEARNING_ENABLED`` env var beats settings.json.
- **AC4** — ``set_memory_setting("learning_enabled", ...)`` round-trips and
  rejects non-bool values.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest


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
# AC1 — is_learning_enabled() reflects the settings flag, defaults False
# ---------------------------------------------------------------------------


class TestIsLearningEnabledFlag:
    def test_defaults_to_false_when_absent(self, settings_file: Path) -> None:
        from cli_agent_orchestrator.services.settings_service import is_learning_enabled

        assert not settings_file.exists()
        assert is_learning_enabled() is False

    def test_returns_true_when_explicitly_enabled(self, settings_file: Path) -> None:
        from cli_agent_orchestrator.services.settings_service import is_learning_enabled

        settings_file.write_text(json.dumps({"memory": {"learning_enabled": True}}))
        assert is_learning_enabled() is True

    def test_returns_false_when_explicitly_disabled(self, settings_file: Path) -> None:
        from cli_agent_orchestrator.services.settings_service import is_learning_enabled

        settings_file.write_text(json.dumps({"memory": {"learning_enabled": False}}))
        assert is_learning_enabled() is False

    def test_get_memory_settings_includes_learning_enabled(self, settings_file: Path) -> None:
        from cli_agent_orchestrator.services.settings_service import get_memory_settings

        settings = get_memory_settings()
        assert settings["learning_enabled"] is False


# ---------------------------------------------------------------------------
# AC2 — memory.enabled=False forces learning off
# ---------------------------------------------------------------------------


class TestLearningRequiresMemory:
    def test_disabled_memory_forces_learning_off(self, settings_file: Path) -> None:
        from cli_agent_orchestrator.services.settings_service import is_learning_enabled

        settings_file.write_text(
            json.dumps({"memory": {"enabled": False, "learning_enabled": True}})
        )
        assert is_learning_enabled() is False

    def test_enabled_memory_with_learning_on(self, settings_file: Path) -> None:
        from cli_agent_orchestrator.services.settings_service import is_learning_enabled

        settings_file.write_text(
            json.dumps({"memory": {"enabled": True, "learning_enabled": True}})
        )
        assert is_learning_enabled() is True

    def test_memory_env_disable_forces_learning_off(self, settings_file: Path) -> None:
        from cli_agent_orchestrator.services.settings_service import is_learning_enabled

        settings_file.write_text(json.dumps({"memory": {"learning_enabled": True}}))
        with patch.dict("os.environ", {"CAO_MEMORY_ENABLED": "false"}):
            assert is_learning_enabled() is False


# ---------------------------------------------------------------------------
# AC3 — CAO_MEMORY_LEARNING_ENABLED env var beats settings.json
# ---------------------------------------------------------------------------


class TestLearningEnvOverride:
    def test_env_true_beats_file_absent(self, settings_file: Path) -> None:
        from cli_agent_orchestrator.services.settings_service import is_learning_enabled

        with patch.dict("os.environ", {"CAO_MEMORY_LEARNING_ENABLED": "true"}):
            assert is_learning_enabled() is True

    def test_env_false_beats_file_true(self, settings_file: Path) -> None:
        from cli_agent_orchestrator.services.settings_service import is_learning_enabled

        settings_file.write_text(json.dumps({"memory": {"learning_enabled": True}}))
        with patch.dict("os.environ", {"CAO_MEMORY_LEARNING_ENABLED": "0"}):
            assert is_learning_enabled() is False

    def test_env_accepts_1_yes_true(self, settings_file: Path) -> None:
        from cli_agent_orchestrator.services.settings_service import is_learning_enabled

        for raw in ("1", "true", "yes", "TRUE", "Yes"):
            with patch.dict("os.environ", {"CAO_MEMORY_LEARNING_ENABLED": raw}):
                assert is_learning_enabled() is True, f"env value {raw!r} should enable"

    def test_env_blank_falls_through_to_file(self, settings_file: Path) -> None:
        from cli_agent_orchestrator.services.settings_service import is_learning_enabled

        settings_file.write_text(json.dumps({"memory": {"learning_enabled": True}}))
        with patch.dict("os.environ", {"CAO_MEMORY_LEARNING_ENABLED": "  "}):
            assert is_learning_enabled() is True


# ---------------------------------------------------------------------------
# AC4 — set_memory_setting round-trip + validation
# ---------------------------------------------------------------------------


class TestSetLearningEnabled:
    def test_round_trip(self, settings_file: Path) -> None:
        from cli_agent_orchestrator.services.settings_service import (
            get_memory_settings,
            is_learning_enabled,
            set_memory_setting,
        )

        set_memory_setting("learning_enabled", True)
        assert is_learning_enabled() is True
        assert get_memory_settings()["learning_enabled"] is True

        set_memory_setting("learning_enabled", False)
        assert is_learning_enabled() is False

    def test_rejects_non_bool(self, settings_file: Path) -> None:
        from cli_agent_orchestrator.services.settings_service import set_memory_setting

        with pytest.raises(ValueError, match="learning_enabled must be a bool"):
            set_memory_setting("learning_enabled", "yes")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AC5 — instruction_promotion_enabled: promotion ⊂ learning ⊂ memory
# ---------------------------------------------------------------------------


class TestInstructionPromotionFlag:
    def test_defaults_to_false(self, settings_file: Path) -> None:
        from cli_agent_orchestrator.services.settings_service import (
            is_instruction_promotion_enabled,
        )

        assert is_instruction_promotion_enabled() is False

    def test_requires_learning_enabled(self, settings_file: Path) -> None:
        from cli_agent_orchestrator.services.settings_service import (
            is_instruction_promotion_enabled,
        )

        # Promotion on but learning off → forced off.
        settings_file.write_text(json.dumps({"memory": {"instruction_promotion_enabled": True}}))
        assert is_instruction_promotion_enabled() is False

    def test_full_chain_enabled(self, settings_file: Path) -> None:
        from cli_agent_orchestrator.services.settings_service import (
            is_instruction_promotion_enabled,
        )

        settings_file.write_text(
            json.dumps(
                {
                    "memory": {
                        "enabled": True,
                        "learning_enabled": True,
                        "instruction_promotion_enabled": True,
                    }
                }
            )
        )
        assert is_instruction_promotion_enabled() is True

    def test_memory_off_forces_promotion_off(self, settings_file: Path) -> None:
        from cli_agent_orchestrator.services.settings_service import (
            is_instruction_promotion_enabled,
        )

        settings_file.write_text(
            json.dumps(
                {
                    "memory": {
                        "enabled": False,
                        "learning_enabled": True,
                        "instruction_promotion_enabled": True,
                    }
                }
            )
        )
        assert is_instruction_promotion_enabled() is False

    def test_env_override(self, settings_file: Path) -> None:
        from cli_agent_orchestrator.services.settings_service import (
            is_instruction_promotion_enabled,
        )

        settings_file.write_text(json.dumps({"memory": {"learning_enabled": True}}))
        with patch.dict("os.environ", {"CAO_MEMORY_INSTRUCTION_PROMOTION_ENABLED": "true"}):
            assert is_instruction_promotion_enabled() is True

    def test_set_round_trip_and_validation(self, settings_file: Path) -> None:
        from cli_agent_orchestrator.services.settings_service import (
            get_memory_settings,
            set_memory_setting,
        )

        set_memory_setting("instruction_promotion_enabled", True)
        assert get_memory_settings()["instruction_promotion_enabled"] is True
        with pytest.raises(ValueError, match="instruction_promotion_enabled must be a bool"):
            set_memory_setting("instruction_promotion_enabled", 1)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AC6 — read errors fail closed (opt-in features)
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_learning_fails_closed_on_settings_error(self, settings_file: Path) -> None:
        from cli_agent_orchestrator.services import settings_service

        with patch.object(
            settings_service, "get_memory_settings", side_effect=RuntimeError("boom")
        ):
            assert settings_service.is_learning_enabled() is False

    def test_promotion_fails_closed_on_settings_error(self, settings_file: Path) -> None:
        from cli_agent_orchestrator.services import settings_service

        settings_file.write_text(json.dumps({"memory": {"learning_enabled": True}}))
        with patch.object(
            settings_service, "get_memory_settings", side_effect=RuntimeError("boom")
        ):
            assert settings_service.is_instruction_promotion_enabled() is False
