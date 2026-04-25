"""Settings service for persisting user configuration."""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from cli_agent_orchestrator.constants import CAO_HOME_DIR

logger = logging.getLogger(__name__)

SETTINGS_FILE = CAO_HOME_DIR / "settings.json"

# Default agent directories per provider
_DEFAULTS = {
    "kiro_cli": str(Path.home() / ".kiro" / "agents"),
    "q_cli": str(Path.home() / ".aws" / "amazonq" / "cli-agents"),
    "claude_code": str(Path.home() / ".aws" / "cli-agent-orchestrator" / "agent-store"),
    "codex": str(Path.home() / ".aws" / "cli-agent-orchestrator" / "agent-store"),
    "cao_installed": str(Path.home() / ".aws" / "cli-agent-orchestrator" / "agent-context"),
}


def _load() -> Dict[str, Any]:
    """Load settings from disk."""
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text())
        except Exception as e:
            logger.warning(f"Failed to read settings: {e}")
    return {}


def _save(data: Dict[str, Any]) -> None:
    """Save settings to disk."""
    CAO_HOME_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data, indent=2))


def get_agent_dirs() -> Dict[str, str]:
    """Get configured agent directories per provider.

    Returns dict like:
      {"kiro_cli": "/home/user/.kiro/agents", "q_cli": "...", ...}
    """
    settings = _load()
    saved = settings.get("agent_dirs", {})
    # Merge defaults with saved — saved overrides defaults
    result = dict(_DEFAULTS)
    result.update(saved)
    return result


def set_agent_dirs(dirs: Dict[str, str]) -> Dict[str, str]:
    """Update agent directories. Only updates providers that are specified."""
    settings = _load()
    current = settings.get("agent_dirs", {})
    for provider, path in dirs.items():
        if provider in _DEFAULTS:
            current[provider] = path
    settings["agent_dirs"] = current
    _save(settings)
    logger.info(f"Updated agent directories: {current}")
    return get_agent_dirs()


def get_extra_agent_dirs() -> List[str]:
    """Get extra agent scan directories (user-added custom paths)."""
    settings = _load()
    return settings.get("extra_agent_dirs", [])


def get_memory_settings() -> Dict[str, Any]:
    """Get memory-related settings.

    Returns dict like:
      {"enabled": True, "flush_threshold": 0.85}

    ``enabled`` defaults to ``True`` (opt-out) to preserve current shipping
    behavior. Setting it to ``False`` disables all memory subsystem
    operations — see ``is_memory_enabled()``.
    """
    settings = _load()
    defaults: Dict[str, Any] = {"enabled": True, "flush_threshold": 0.85}
    saved = settings.get("memory", {})
    result = dict(defaults)
    result.update(saved)
    return result


def is_memory_enabled() -> bool:
    """Return True when the memory subsystem is enabled.

    Reads the ``memory.enabled`` flag from settings; defaults to True
    (opt-out) so existing installations preserve current behavior.
    """
    try:
        value = get_memory_settings().get("enabled", True)
    except Exception as e:
        logger.warning(f"Failed to read memory.enabled, defaulting to True: {e}")
        return True
    return bool(value)


def get_project_id_override() -> Optional[str]:
    """Return an explicit project identity override, or None when not set.

    Precedence (Phase 2.5 U6):
        1. ``CAO_PROJECT_ID`` environment variable.
        2. ``memory.project_id`` nested key in settings.json — matches the
           ``memory.enabled`` precedent from U5.

    The raw value is returned untouched; validation (whitelist + null-byte
    reject) happens in ``memory_service._validate_project_id_override`` so a
    bad value surfaces a clear error at resolve time.
    """
    import os as _os

    env_val = _os.environ.get("CAO_PROJECT_ID")
    if env_val and env_val.strip():
        return env_val.strip()

    memory_settings = get_memory_settings()
    val = memory_settings.get("project_id")
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def set_memory_setting(key: str, value: Any) -> Dict[str, Any]:
    """Update a single memory setting.

    Supported keys:
        ``enabled`` (bool) — master switch for the memory subsystem.
        ``flush_threshold`` (float, 0.0 < x ≤ 1.0) — context-usage trigger.
    """
    settings = _load()
    memory = settings.get("memory", {})

    if key == "enabled":
        if not isinstance(value, bool):
            raise ValueError(f"enabled must be a bool, got {type(value).__name__}")
        memory[key] = value
    elif key == "flush_threshold":
        fval = float(value)
        if not (0.0 < fval <= 1.0):
            raise ValueError(f"flush_threshold must be between 0.0 and 1.0, got {fval}")
        memory[key] = fval
    else:
        raise ValueError(f"Unknown memory setting: {key}")

    settings["memory"] = memory
    _save(settings)
    logger.info(f"Updated memory setting: {key}={memory[key]}")
    return get_memory_settings()


def set_extra_agent_dirs(dirs: List[str]) -> List[str]:
    """Set extra agent scan directories."""
    settings = _load()
    settings["extra_agent_dirs"] = [d for d in dirs if d.strip()]
    _save(settings)
    return settings["extra_agent_dirs"]
