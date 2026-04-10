"""Settings service for persisting user configuration."""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, cast

from cli_agent_orchestrator.constants import CAO_HOME_DIR

logger = logging.getLogger(__name__)

SETTINGS_FILE = CAO_HOME_DIR / "settings.json"

# Default agent directories per provider
_DEFAULTS: Dict[str, str] = {
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
            return cast(Dict[str, Any], json.loads(SETTINGS_FILE.read_text()))
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
    extra_dirs = settings.get("extra_agent_dirs", [])
    return [str(path) for path in extra_dirs]


def set_extra_agent_dirs(dirs: List[str]) -> List[str]:
    """Set extra agent scan directories."""
    settings = _load()
    settings["extra_agent_dirs"] = [d for d in dirs if d.strip()]
    _save(settings)
    return [str(path) for path in settings["extra_agent_dirs"]]
