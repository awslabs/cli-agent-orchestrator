"""Settings service for persisting user configuration."""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from cli_agent_orchestrator.constants import CAO_HOME_DIR

logger = logging.getLogger(__name__)

SETTINGS_FILE = CAO_HOME_DIR / "settings.json"

# Default agent directories per provider
_DEFAULTS = {
    "kiro_cli": str(Path.home() / ".kiro" / "agents"),
    "claude_code": str(Path.home() / ".aws" / "cli-agent-orchestrator" / "agent-store"),
    "codex": str(Path.home() / ".aws" / "cli-agent-orchestrator" / "agent-store"),
    "cao_installed": str(Path.home() / ".aws" / "cli-agent-orchestrator" / "agent-context"),
}


def _load() -> Dict[str, Any]:
    """Load settings from disk."""
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text())
            if isinstance(data, dict):
                return data
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
      {"kiro_cli": "/home/user/.kiro/agents", "claude_code": "...", ...}
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


# Default server tuning values
_SERVER_DEFAULTS = {
    "mcp_request_timeout": 30,
    "event_bus_max_queue_size": 1024,
    "provider_init_timeout": 60,
    "startup_prompt_handler_timeout": 20,
}


_server_settings_cache: Optional[Dict[str, Any]] = None
_server_settings_mtime_ns: int = -1


def get_server_settings() -> Dict[str, Any]:
    """Get server tuning settings (cached; re-reads only when file changes).

    Returns a dict with the following keys (defaults shown):
      - mcp_request_timeout (30): Seconds to wait for MCP HTTP calls
      - event_bus_max_queue_size (1024): Max events buffered per subscriber
      - provider_init_timeout (60): Seconds to wait for a CLI agent to reach IDLE
      - startup_prompt_handler_timeout (20): Seconds to handle startup prompts
        (e.g., workspace trust dialogs) before giving up

    Values can be set in ~/.aws/cli-agent-orchestrator/settings.json under
    the "server" key:

        {
          "server": {
            "mcp_request_timeout": 120,
            "event_bus_max_queue_size": 8192,
            "provider_init_timeout": 90,
            "startup_prompt_handler_timeout": 5
          }
        }
    """
    global _server_settings_cache, _server_settings_mtime_ns
    # Cache: only re-read when the file has changed
    try:
        mtime_ns = SETTINGS_FILE.stat().st_mtime_ns if SETTINGS_FILE.exists() else -1
    except OSError:
        mtime_ns = -1
    if _server_settings_cache is not None and mtime_ns == _server_settings_mtime_ns:
        return dict(_server_settings_cache)

    settings = _load()
    saved = settings.get("server", {})
    if not isinstance(saved, dict):
        logger.warning("Invalid settings.server=%r (expected object); using defaults", saved)
        saved = {}
    result = dict(_SERVER_DEFAULTS)
    result.update({k: v for k, v in saved.items() if k in _SERVER_DEFAULTS})
    # Validate types and ranges; coerce to int for queue size
    for key, default in _SERVER_DEFAULTS.items():
        val = result[key]
        if isinstance(val, bool) or not isinstance(val, (int, float)) or val <= 0:
            logger.warning(f"Invalid server setting {key}={val!r}, using default {default}")
            result[key] = default
    result["event_bus_max_queue_size"] = int(result["event_bus_max_queue_size"])
    _server_settings_cache = result
    _server_settings_mtime_ns = mtime_ns
    return dict(result)


def get_memory_settings() -> Dict[str, Any]:
    """Get memory-related settings.

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

    Reads the ``memory.enabled`` flag; defaults to True (opt-out) so
    existing installations preserve current behavior.
    """
    try:
        value = get_memory_settings().get("enabled", True)
    except Exception as e:
        logger.warning(f"Failed to read memory.enabled, defaulting to True: {e}")
        return True
    return bool(value)


def get_compile_mode() -> str:
    """Return the active wiki-compilation mode.

    Precedence:
        1. ``CAO_MEMORY_COMPILE_MODE`` env var (case-insensitive). Accepted
           values: ``llm``, ``append``. Unknown values are ignored with a
           WARNING and fall through to settings/default.
        2. ``memory.compile_mode`` nested key in settings.json.
        3. Default ``"llm"``.

    Read errors fall through to ``"append"`` — the safe default that never
    invokes the LLM and reproduces Phase 1/2 behaviour.
    """
    env_raw = os.environ.get("CAO_MEMORY_COMPILE_MODE")
    if env_raw is not None:
        v = env_raw.strip().lower()
        if v in ("llm", "append"):
            return v
        if v != "":
            logger.warning(
                f"Ignoring unknown CAO_MEMORY_COMPILE_MODE={env_raw!r}; "
                "falling through to settings.json"
            )
    try:
        value = get_memory_settings().get("compile_mode", "llm")
    except Exception as e:
        logger.warning(f"Failed to read memory.compile_mode, defaulting to append: {e}")
        return "append"
    if isinstance(value, str) and value.strip().lower() in ("llm", "append"):
        return value.strip().lower()
    return "append"


def get_compile_timeout_s() -> float:
    """Return the wall-clock timeout (seconds) for the wiki compile call.

    Generous by default: compilation drives a coding-agent CLI that can
    cold-start in tens of seconds, and it runs in the background so the
    timeout never blocks store().
    """
    try:
        value = get_memory_settings().get("compile_timeout_s", 120.0)
        return float(value)
    except Exception as e:
        logger.warning(f"Failed to read memory.compile_timeout_s, defaulting to 120.0: {e}")
        return 120.0


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


def get_extra_agent_dirs() -> List[str]:
    """Get extra agent scan directories (user-added custom paths)."""
    settings = _load()
    dirs = settings.get("extra_agent_dirs", [])
    return dirs if isinstance(dirs, list) else []


def set_extra_agent_dirs(dirs: List[str]) -> List[str]:
    """Set extra agent scan directories."""
    settings = _load()
    extra_agent_dirs = [d for d in dirs if d.strip()]
    settings["extra_agent_dirs"] = extra_agent_dirs
    _save(settings)
    return extra_agent_dirs


def get_extra_skill_dirs() -> List[str]:
    """Get extra skill scan directories (user-added custom paths).

    Filters to non-empty strings so malformed persisted data (e.g. a manually
    edited ``settings.json`` storing ``null`` or numbers) cannot later raise a
    ``TypeError`` from ``Path(extra)`` and break skill listing/loading.
    """
    settings = _load()
    dirs = settings.get("extra_skill_dirs", [])
    if not isinstance(dirs, list):
        return []
    return [d.strip() for d in dirs if isinstance(d, str) and d.strip()]


def set_extra_skill_dirs(dirs: List[str]) -> List[str]:
    """Set extra skill scan directories."""
    settings = _load()
    extra_skill_dirs = [d.strip() for d in dirs if isinstance(d, str) and d.strip()]
    settings["extra_skill_dirs"] = extra_skill_dirs
    _save(settings)
    return extra_skill_dirs
