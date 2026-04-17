"""Hook registration logic for CAO memory self-save hooks.

Installs hook scripts to ~/.aws/cli-agent-orchestrator/hooks/ on server startup
and registers them with provider-specific config files on terminal creation.
"""

import json
import logging
import os
import re
import shutil
from pathlib import Path

from cli_agent_orchestrator.constants import CAO_HOME_DIR

# Safe characters for agent profile names (no path separators or special chars)
_SAFE_PROFILE_RE = re.compile(r"^[a-zA-Z0-9_\-]+$")


def _resolve_working_directory(working_directory: str) -> Path:
    """Resolve and validate a working directory path to prevent path traversal.

    Resolves symlinks via realpath and verifies the result is an absolute path
    before it is used to construct config file paths.

    Raises:
        ValueError: If the resolved path is not absolute or contains null bytes.
    """
    if "\x00" in working_directory:
        raise ValueError("Working directory contains null bytes")
    resolved = Path(os.path.realpath(working_directory))
    if not resolved.is_absolute():
        raise ValueError(f"Working directory resolved to a non-absolute path: {resolved}")
    return resolved


def _sanitize_agent_profile(agent_profile: str) -> str:
    """Sanitize an agent profile name to prevent path traversal.

    Strips directory separators and validates only safe characters remain.
    The result is used as a filename component under KIRO_AGENTS_DIR.

    Raises:
        ValueError: If the profile name contains unsafe characters.
    """
    if "\x00" in agent_profile:
        raise ValueError("Agent profile name contains null bytes")
    # Strip any directory components — only the basename matters
    basename = os.path.basename(agent_profile)
    if not _SAFE_PROFILE_RE.match(basename):
        raise ValueError(
            f"Invalid agent profile name '{basename}': only alphanumeric, hyphens, and underscores allowed"
        )
    return basename

logger = logging.getLogger(__name__)

HOOKS_INSTALL_DIR = CAO_HOME_DIR / "hooks"

# Source directory for hook scripts (package-relative)
_HOOKS_SRC_DIR = Path(__file__).parent

STOP_HOOK_SCRIPT = "cao_stop_hook.sh"
PRECOMPACT_HOOK_SCRIPT = "cao_precompact_hook.sh"
KIRO_SPAWN_HOOK_SCRIPT = "cao_kiro_spawn_hook.sh"
KIRO_PROMPT_HOOK_SCRIPT = "cao_kiro_prompt_hook.sh"

STOP_HOOK_PATH = HOOKS_INSTALL_DIR / STOP_HOOK_SCRIPT
PRECOMPACT_HOOK_PATH = HOOKS_INSTALL_DIR / PRECOMPACT_HOOK_SCRIPT
KIRO_SPAWN_HOOK_PATH = HOOKS_INSTALL_DIR / KIRO_SPAWN_HOOK_SCRIPT
KIRO_PROMPT_HOOK_PATH = HOOKS_INSTALL_DIR / KIRO_PROMPT_HOOK_SCRIPT


def install_hooks() -> None:
    """Copy hook scripts to ~/.aws/cli-agent-orchestrator/hooks/ and chmod +x.

    Idempotent — safe to call on every server startup.
    """
    HOOKS_INSTALL_DIR.mkdir(parents=True, exist_ok=True)

    for script_name in (
        STOP_HOOK_SCRIPT,
        PRECOMPACT_HOOK_SCRIPT,
        KIRO_SPAWN_HOOK_SCRIPT,
        KIRO_PROMPT_HOOK_SCRIPT,
    ):
        src = _HOOKS_SRC_DIR / script_name
        dst = HOOKS_INSTALL_DIR / script_name
        if not src.exists():
            logger.warning(f"Hook script source not found: {src}")
            continue
        shutil.copy2(src, dst)
        dst.chmod(0o755)
        logger.info(f"Installed hook script: {dst}")


def register_hooks_claude_code(working_directory: str) -> None:
    """Merge CAO hooks into .claude/settings.local.json for Claude Code.

    Merges Stop and PreCompact hooks without overwriting existing user hooks.

    Args:
        working_directory: The terminal's working directory (where .claude/ lives).
    """
    resolved_dir = _resolve_working_directory(working_directory)
    settings_path = resolved_dir / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning(f"Could not parse {settings_path}, starting fresh")

    hooks = existing.setdefault("hooks", {})

    # Define CAO hook entries
    cao_stop_entry = {
        "matcher": "",
        "hooks": [
            {
                "type": "command",
                "command": str(STOP_HOOK_PATH),
            }
        ],
    }
    cao_precompact_entry = {
        "matcher": "",
        "hooks": [
            {
                "type": "command",
                "command": str(PRECOMPACT_HOOK_PATH),
            }
        ],
    }

    # Merge Stop hook — append if not already present
    stop_hooks = hooks.setdefault("Stop", [])
    if not _hook_entry_exists(stop_hooks, str(STOP_HOOK_PATH)):
        stop_hooks.append(cao_stop_entry)

    # Merge PreCompact hook — append if not already present
    precompact_hooks = hooks.setdefault("PreCompact", [])
    if not _hook_entry_exists(precompact_hooks, str(PRECOMPACT_HOOK_PATH)):
        precompact_hooks.append(cao_precompact_entry)

    settings_path.write_text(json.dumps(existing, indent=2) + "\n")
    logger.info(f"Registered CAO hooks in {settings_path}")


_CAO_HOOK_COMMANDS = {str(KIRO_SPAWN_HOOK_PATH), str(KIRO_PROMPT_HOOK_PATH)}


def register_hooks_kiro(agent_profile: str) -> None:
    """Write CAO hooks into ~/.kiro/agents/{agent_profile}.json for Kiro CLI.

    Self-healing: removes any existing CAO hook entries (any format/version)
    then writes the correct current entries. Safe to call on every launch.

    Args:
        agent_profile: The Kiro agent profile name (maps to ~/.kiro/agents/{name}.json).
    """
    from cli_agent_orchestrator.constants import KIRO_AGENTS_DIR

    safe_profile = _sanitize_agent_profile(agent_profile)
    agent_config_path = KIRO_AGENTS_DIR / f"{safe_profile}.json"
    # Verify resolved path stays within KIRO_AGENTS_DIR to prevent traversal
    if not str(agent_config_path.resolve()).startswith(str(KIRO_AGENTS_DIR.resolve())):
        raise ValueError(f"Agent profile path escapes agents directory: {agent_config_path}")
    if not agent_config_path.exists():
        logger.warning(f"Kiro agent config not found: {agent_config_path}, skipping hook registration")
        return

    existing: dict = {}
    try:
        existing = json.loads(agent_config_path.read_text())
    except (json.JSONDecodeError, OSError):
        logger.warning(f"Could not parse {agent_config_path}, skipping hook registration")
        return

    hooks = existing.setdefault("hooks", {})

    # Remove all existing CAO entries from every event key (handles stale/old-format entries)
    for event_key in list(hooks.keys()):
        hooks[event_key] = [
            e for e in hooks[event_key]
            if isinstance(e, dict) and e.get("command") not in _CAO_HOOK_COMMANDS
        ]
        if not hooks[event_key]:
            del hooks[event_key]

    # Write correct current entries
    hooks.setdefault("agentSpawn", []).append({"command": str(KIRO_SPAWN_HOOK_PATH)})
    hooks.setdefault("userPromptSubmit", []).append({"command": str(KIRO_PROMPT_HOOK_PATH)})

    agent_config_path.write_text(json.dumps(existing, indent=2) + "\n")
    logger.info(f"Registered CAO hooks in {agent_config_path}")


def register_hooks_codex(working_directory: str) -> None:
    """Merge CAO stop hook into .codex/hooks.json for Codex.

    Args:
        working_directory: The terminal's working directory (where .codex/ lives).
    """
    resolved_dir = _resolve_working_directory(working_directory)
    hooks_path = resolved_dir / ".codex" / "hooks.json"
    hooks_path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict = {}
    if hooks_path.exists():
        try:
            existing = json.loads(hooks_path.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning(f"Could not parse {hooks_path}, starting fresh")

    # Only set stop hook if not already configured
    if "stop" not in existing:
        existing["stop"] = str(STOP_HOOK_PATH)

    hooks_path.write_text(json.dumps(existing, indent=2) + "\n")
    logger.info(f"Registered CAO hooks in {hooks_path}")


def _hook_entry_exists(hook_list: list, command_path: str) -> bool:
    """Check if a hook entry with the given command path already exists.

    Handles two formats:
    - Kiro flat format: [{"command": "...", "timeout_ms": N}]
    - Claude Code nested format: [{"matcher": "", "hooks": [{"command": "..."}]}]
    """
    for entry in hook_list:
        if not isinstance(entry, dict):
            continue
        # Kiro flat format
        if entry.get("command") == command_path:
            return True
        # Claude Code nested format
        for hook in entry.get("hooks", []):
            if isinstance(hook, dict) and hook.get("command") == command_path:
                return True
    return False
