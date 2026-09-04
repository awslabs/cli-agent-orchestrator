"""Settings service for persisting user configuration."""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from cli_agent_orchestrator.constants import CAO_HOME_DIR
from cli_agent_orchestrator.utils.paths import normalized_path

logger = logging.getLogger(__name__)

SETTINGS_FILE = CAO_HOME_DIR / "settings.json"

# Default agent directories per provider
_DEFAULTS = {
    "kiro_cli": str(Path.home() / ".kiro" / "agents"),
    "claude_code": str(CAO_HOME_DIR / "agent-store"),
    "codex": str(CAO_HOME_DIR / "agent-store"),
    "cao_installed": str(CAO_HOME_DIR / "agent-context"),
}

_BOOL_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_BOOL_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


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

    Reads from the nested schema first (``agents.dirs``), falls back to the
    legacy flat key (``agent_dirs``) for backward compatibility.

    Returns dict like:
      {"kiro_cli": "/home/user/.kiro/agents", "claude_code": "...", ...}
    """
    settings = _load()
    # Nested format (documented schema): {"agents": {"dirs": {...}}}
    nested = settings.get("agents", {})
    if isinstance(nested, dict) and "dirs" in nested and isinstance(nested["dirs"], dict):
        saved = nested["dirs"]
    else:
        # Legacy flat format: {"agent_dirs": {...}}
        saved = settings.get("agent_dirs", {})
    # Merge defaults with saved — saved overrides defaults
    result = dict(_DEFAULTS)
    result.update(saved)
    return result


def set_agent_dirs(dirs: Dict[str, str]) -> Dict[str, str]:
    """Update agent directories. Only updates providers that are specified.

    Writes to the nested schema format (``agents.dirs``). Also updates the
    legacy flat key (``agent_dirs``) for backward compatibility with older
    CAO versions that may still read it.
    """
    settings = _load()
    # Read current from nested first, fall back to flat
    nested = settings.get("agents", {})
    if isinstance(nested, dict) and "dirs" in nested and isinstance(nested["dirs"], dict):
        current = nested["dirs"]
    else:
        current = settings.get("agent_dirs", {})
    for provider, path in dirs.items():
        if provider in _DEFAULTS:
            current[provider] = path
    # Write nested format
    agents_section = settings.get("agents", {})
    if not isinstance(agents_section, dict):
        agents_section = {}
    agents_section["dirs"] = current
    settings["agents"] = agents_section
    # Also write flat key for backward compat
    settings["agent_dirs"] = current
    _save(settings)
    logger.info(f"Updated agent directories: {current}")
    return get_agent_dirs()


def get_disabled_agent_dirs() -> List[str]:
    """Directory paths the user has toggled OFF.

    A disabled directory stays listed in Settings but is skipped when scanning
    for (and loading) agent profiles, so its profiles disappear from the active
    set without editing paths. Covers both provider defaults (fixes GH #281 —
    a removed default used to silently reappear) and user extras (GH #280).

    Reads from the nested schema first (``agents.disabled_dirs``), falls back
    to the legacy flat key (``disabled_agent_dirs``) — same contract as the
    sibling ``agents.dirs`` / ``agents.extra_dirs`` settings.
    """
    settings = _load()
    nested = settings.get("agents", {})
    if isinstance(nested, dict) and isinstance(nested.get("disabled_dirs"), list):
        # list(...) narrows the untyped-dict Any to list[str] for mypy's
        # no-any-return gate, matching get_extra_agent_dirs' return guard.
        return list(nested["disabled_dirs"])
    dirs = settings.get("disabled_agent_dirs", [])
    return dirs if isinstance(dirs, list) else []


def set_disabled_agent_dirs(dirs: List[str]) -> List[str]:
    """Persist which configured directories are disabled.

    Only paths that are actually configured (a provider default from
    ``get_agent_dirs`` or a user extra) are accepted — an arbitrary path would
    silently match nothing during scanning, so rejecting it keeps the stored
    state honest and the UI truthful. Validation uses the same path
    normalization as the scan/load side (``utils.paths.normalized_path``), so
    a valid directory sent in a different spelling (trailing slash, ``~``,
    symlink) is accepted rather than silently dropped; what gets PERSISTED is
    always the configured spelling, so the UI's exact-string matching keeps
    working. Order and duplicates are normalized away.

    Writes to the nested schema (``agents.disabled_dirs``) and the legacy flat
    key (``disabled_agent_dirs``) for backward compatibility — mirroring
    ``set_agent_dirs`` / ``set_extra_agent_dirs``.
    """
    norm_to_configured = {
        normalized_path(v): v
        for v in list(get_agent_dirs().values()) + list(get_extra_agent_dirs())
        if isinstance(v, str)
    }
    seen: Set[str] = set()
    cleaned: List[str] = []
    for d in dirs:
        if not isinstance(d, str) or not d.strip():
            continue
        configured = norm_to_configured.get(normalized_path(d.strip()))
        if configured is not None and configured not in seen:
            seen.add(configured)
            cleaned.append(configured)
    settings = _load()
    # Write nested format
    agents_section = settings.get("agents", {})
    if not isinstance(agents_section, dict):
        agents_section = {}
    agents_section["disabled_dirs"] = cleaned
    settings["agents"] = agents_section
    # Also write flat key for backward compat
    settings["disabled_agent_dirs"] = cleaned
    _save(settings)
    logger.info(f"Disabled agent dirs: {cleaned}")
    return cleaned


# Default server tuning values
_SERVER_DEFAULTS = {
    "mcp_request_timeout": 30,
    "event_bus_max_queue_size": 1024,
    "provider_init_timeout": 60,
    "startup_prompt_handler_timeout": 20,
    # Rolling per-terminal raw-output buffer StatusMonitor keeps for raw-path
    # status detection and GET /terminals/{id}/output (mode=full) — see
    # StatusMonitor._process_chunk. The old fixed 8192 was measured too small
    # against the pyte screen geometry this buffer has to outlive: a single
    # full-screen repaint of a 220x50 viewport is already ~11,000 visible
    # characters before per-line/per-cell ANSI escapes are added on top, so
    # one repaint alone could exceed the old cap. A long, chatty session with
    # several such repaints (status-bar refreshes, spinner frames, full menu
    # redraws) could evict a still-pending prompt from the buffer before it
    # was ever read back. 32KB covers several trailing repaints instead of a
    # fraction of one, while staying a deliberately bounded trade-off (not
    # unbounded scrollback) — configurable rather than a second blind guess,
    # since the "safe" size is provider/workload-dependent, not one constant.
    "state_buffer_max": 32768,
}

# Env-var overrides for server settings. Precedence: env var > settings.json > default.
_SERVER_ENV_VARS = {
    "mcp_request_timeout": "CAO_MCP_REQUEST_TIMEOUT",
    "event_bus_max_queue_size": "CAO_EVENT_BUS_MAX_QUEUE_SIZE",
    "provider_init_timeout": "CAO_PROVIDER_INIT_TIMEOUT",
    "startup_prompt_handler_timeout": "CAO_STARTUP_PROMPT_HANDLER_TIMEOUT",
    "state_buffer_max": "CAO_STATE_BUFFER_MAX",
}


_server_settings_cache: Optional[Dict[str, Any]] = None
_server_settings_mtime_ns: int = -1


def get_server_settings() -> Dict[str, Any]:
    """Get server tuning settings (cached; re-reads only when file changes).

    Precedence per key: CAO_* env var > settings.json > built-in default.

    Returns a dict with the following keys (defaults shown):
      - mcp_request_timeout (30): Seconds to wait for MCP HTTP calls
      - event_bus_max_queue_size (1024): Max events buffered per subscriber
      - provider_init_timeout (60): Seconds to wait for a CLI agent to reach IDLE.
        Also the hard outer cap on total time the startup-prompt handler may run.
      - startup_prompt_handler_timeout (20): Idle gap, in seconds, between
        consecutive startup prompts (e.g. workspace trust / bypass dialogs). The
        handler keeps polling and resets this timer every time it answers a
        prompt; it stops once no new prompt appears for this many seconds (so a
        dialog a cold/containerized start renders late is still handled). Total
        time is bounded by provider_init_timeout.
      - state_buffer_max (32768): Bytes of raw terminal output StatusMonitor
        keeps per terminal for raw-path status detection and
        GET /terminals/{id}/output (mode=full)

    Values can be set via CAO_* environment variables or in
    ~/.aws/cli-agent-orchestrator/settings.json under the "server" key:

        {
          "server": {
            "mcp_request_timeout": 120,
            "event_bus_max_queue_size": 8192,
            "provider_init_timeout": 90,
            "startup_prompt_handler_timeout": 5,
            "state_buffer_max": 65536
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

    # Env-var overlay: CAO_* env var beats settings.json value.
    for key, env_name in _SERVER_ENV_VARS.items():
        raw = os.environ.get(env_name)
        if raw is not None and raw.strip() != "":
            try:
                result[key] = int(raw)
            except ValueError:
                logger.warning(
                    f"Ignoring invalid {env_name}={raw!r} (expected int); "
                    f"using file/default {result[key]}"
                )

    # Validate types and ranges; coerce to int for queue size
    for key, default in _SERVER_DEFAULTS.items():
        val = result[key]
        # int(val), not val <= 0: a float in (0, 1) (e.g. a settings.json
        # value of 0.5) passes val <= 0 but truncates to 0 once coerced to
        # int below (state_buffer_max) or by Queue(maxsize=...)
        # (event_bus_max_queue_size), reintroducing the same
        # unbounded-buffer/unbounded-queue failure mode this validation
        # exists to prevent. isinstance(val, (int, float)) above guarantees
        # int(val) cannot itself raise here.
        if isinstance(val, bool) or not isinstance(val, (int, float)) or int(val) <= 0:
            logger.warning(f"Invalid server setting {key}={val!r}, using default {default}")
            result[key] = default
    result["event_bus_max_queue_size"] = int(result["event_bus_max_queue_size"])
    # buffer[-state_buffer_max:] requires an int -- a float survives the
    # generic isinstance(val, (int, float)) check above (e.g. a settings.json
    # value of 32768.0), and a float slice bound raises TypeError.
    result["state_buffer_max"] = int(result["state_buffer_max"])
    _server_settings_cache = result
    _server_settings_mtime_ns = mtime_ns
    return dict(result)


def get_max_terminals() -> Optional[int]:
    """Max terminals this node will track, or None for unlimited.

    Precedence: ``CAO_MAX_TERMINALS`` env var > ``server.max_terminals`` in
    settings.json > None (unlimited — the pre-cap default, so existing
    deployments are unaffected).

    Used by ``terminal_service.create_terminal`` to reject creation when the
    node is full. The one-agent-per-pod Kubernetes topology sets
    ``CAO_MAX_TERMINALS=1`` on worker pods so each pod hosts exactly one agent.

    Counts TRACKED terminals, meaning rows in the terminals table, not probed
    liveness: a terminal row is only removed by an explicit ``delete_terminal``,
    so a terminal whose process died without cleanup still occupies a slot. That
    is harmless in the topology the cap exists for (a worker pod is a disposable
    Job, so its rows die with it) but an operator who sets ``server.max_terminals``
    on a long-lived node may have to delete a stale row by hand. Deliberately not
    probed here: this check runs before anything is allocated precisely so it can
    stay cheap, and per-row liveness probing would put tmux calls in that path.

    Not folded into ``_SERVER_DEFAULTS`` because that table's validation forces
    every key to a positive int default; this setting's default is "absent"
    (unlimited), which that machinery cannot represent. Invalid or non-positive
    values are ignored with a warning (unlimited) rather than treated as 0,
    which would brick all terminal creation on a typo.
    """
    raw = os.environ.get("CAO_MAX_TERMINALS")
    source = "CAO_MAX_TERMINALS"
    if raw is None or raw.strip() == "":
        saved = _load().get("server", {})
        raw = saved.get("max_terminals") if isinstance(saved, dict) else None
        source = "server.max_terminals"
        if raw is None:
            return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(f"Ignoring invalid {source}={raw!r} (expected int); terminals unlimited")
        return None
    if value <= 0:
        logger.warning(f"Ignoring non-positive {source}={value}; terminals unlimited")
        return None
    return value


def get_memory_settings() -> Dict[str, Any]:
    """Get memory-related settings.

    Precedence for most keys: CAO_* env var > settings.json > built-in
    default. ``memory.lint_enabled`` intentionally uses
    ``is_memory_lint_enabled()`` fail-closed semantics instead: any explicit
    false in persisted settings or ``CAO_MEMORY_LINT_ENABLED`` disables lint.

    ``enabled`` defaults to ``True`` (opt-out) to preserve current shipping
    behavior. Setting it to ``False`` disables all memory subsystem
    operations — see ``is_memory_enabled()``.
    """
    settings = _load()
    defaults: Dict[str, Any] = {
        "enabled": True,
        "flush_threshold": 0.85,
        "lint_enabled": True,
        "learning_enabled": False,
        "instruction_promotion_enabled": False,
    }
    saved = settings.get("memory", {})
    if not isinstance(saved, dict):
        logger.warning("Invalid settings.memory=%r (expected object); using defaults", saved)
        saved = {}
    result = dict(defaults)
    result.update(saved)

    # Env-var overlay: CAO_MEMORY_ENABLED beats settings.json
    env_enabled = os.environ.get("CAO_MEMORY_ENABLED")
    if env_enabled is not None and env_enabled.strip() != "":
        result["enabled"] = env_enabled.strip().lower() in ("1", "true", "yes")

    # Env-var overlay: CAO_MEMORY_LEARNING_ENABLED beats settings.json
    env_learning = os.environ.get("CAO_MEMORY_LEARNING_ENABLED")
    if env_learning is not None and env_learning.strip() != "":
        result["learning_enabled"] = env_learning.strip().lower() in ("1", "true", "yes")

    # Env-var overlay: CAO_MEMORY_INSTRUCTION_PROMOTION_ENABLED beats settings.json
    env_promotion = os.environ.get("CAO_MEMORY_INSTRUCTION_PROMOTION_ENABLED")
    if env_promotion is not None and env_promotion.strip() != "":
        result["instruction_promotion_enabled"] = env_promotion.strip().lower() in (
            "1",
            "true",
            "yes",
        )

    # Env-var overlay: CAO_MEMORY_FLUSH_THRESHOLD beats settings.json
    env_threshold = os.environ.get("CAO_MEMORY_FLUSH_THRESHOLD")
    if env_threshold is not None and env_threshold.strip() != "":
        try:
            fval = float(env_threshold)
            if 0.0 < fval <= 1.0:
                result["flush_threshold"] = fval
            else:
                logger.warning(
                    f"Ignoring CAO_MEMORY_FLUSH_THRESHOLD={env_threshold!r} "
                    f"(must be between 0.0 and 1.0); using file/default"
                )
        except ValueError:
            logger.warning(
                f"Ignoring invalid CAO_MEMORY_FLUSH_THRESHOLD={env_threshold!r} "
                f"(expected float); using file/default"
            )

    result["lint_enabled"] = is_memory_lint_enabled(settings=settings)
    return result


def _coerce_optional_bool(value: Any, *, label: str) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "":
            return None
        if normalized in _BOOL_TRUE_VALUES:
            return True
        if normalized in _BOOL_FALSE_VALUES:
            return False
    logger.warning("Ignoring invalid %s=%r (expected bool); using file/default", label, value)
    return None


def _explicit_false(value: Any, *, label: str) -> bool:
    return _coerce_optional_bool(value, label=label) is False


def is_memory_lint_enabled(settings: Optional[Dict[str, Any]] = None) -> bool:
    """Return True unless persisted settings or env explicitly disable lint.

    This is intentionally not normal env precedence: either explicit false
    source disables lint, so env true cannot override persisted false and
    persisted true cannot override env false.
    """
    try:
        data = settings if settings is not None else _load()
        saved = data.get("memory", {}) if isinstance(data, dict) else {}
        if not isinstance(saved, dict):
            saved = {}

        if _explicit_false(saved.get("lint_enabled", True), label="memory.lint_enabled"):
            return False

        raw_env = os.environ.get("CAO_MEMORY_LINT_ENABLED")
        if raw_env is not None and raw_env.strip() != "":
            coerced = _coerce_optional_bool(raw_env, label="CAO_MEMORY_LINT_ENABLED")
            if coerced is False:
                return False
        return True
    except Exception as e:
        logger.warning(f"Failed to read memory.lint_enabled, defaulting to True: {e}")
        return True


def is_memory_enabled() -> bool:
    """Return True when the memory subsystem is enabled.

    Precedence: CAO_MEMORY_ENABLED env var > memory.enabled in settings.json
    > default (True).
    """
    try:
        value = get_memory_settings().get("enabled", True)
    except Exception as e:
        logger.warning(f"Failed to read memory.enabled, defaulting to True: {e}")
        return True
    return bool(value)


def is_learning_enabled() -> bool:
    """Return True when workflow self-learning (outcome capture) is enabled.

    Precedence: CAO_MEMORY_LEARNING_ENABLED env var > memory.learning_enabled
    in settings.json > default (False — learning is opt-in).

    Learning is a child of the memory subsystem: lessons distilled from
    outcomes are stored via memory, so a disabled memory subsystem disables
    learning regardless of this flag. Read errors default to False (opt-in
    features fail closed, mirroring the default).
    """
    try:
        settings = get_memory_settings()
        return bool(settings.get("enabled", True)) and bool(settings.get("learning_enabled", False))
    except Exception as e:
        logger.warning(f"Failed to read memory.learning_enabled, defaulting to False: {e}")
        return False


def is_workflow_approval_required() -> bool:
    """Return True when an unapproved script-tier workflow run must be refused (issue #583 FR-8).

    Default is **False**: enforcement is opt-in. A ``plan_id`` does not exist until run start, so a
    plan that has never run cannot have been approved and its first run is refused by design. Making
    that the default would break every existing script-tier caller one Bolt before the authoring
    sequence that presents a plan and takes approval BEFORE running.

    PRECEDENCE IS DELIBERATELY ASYMMETRIC, AND THIS IS NOT AN OVERSIGHT. Every sibling setting here
    resolves ``CAO_* env var > settings.json > default`` — see :func:`is_memory_enabled`. This one
    does NOT:

        ``CAO_WORKFLOW_REQUIRE_APPROVAL`` may turn the gate **ON**.
        Only ``settings.json`` can turn it **OFF**.

    The reason is that this setting is a CONTROL, not a feature toggle. Under the house precedence,
    anything able to set an environment variable could switch the approval gate off while the
    operator's settings file still read as configured on — which would make the weakest
    configuration mechanism in the system the one that decides whether runs are authorised. The
    asymmetry is monotonic in the safe direction: nothing that merely influences an environment can
    weaken the gate, while enabling it for a single test or trial stays a one-liner.

    Read failure resolves to the default (disabled) with a warning, which is the ONE place this
    mechanism is deliberately not fail-closed. Treating an unreadable settings file as "gate on"
    would refuse every script run in the installation on the strength of a JSON typo. Resolving to
    disabled makes the unreadable case behave like the unconfigured case, and the asymmetry above
    bounds the residual: an operator who enabled the gate via the environment is unaffected by a
    corrupt file.
    """
    env = os.environ.get("CAO_WORKFLOW_REQUIRE_APPROVAL")
    if env is not None and env.strip().lower() in ("1", "true", "yes"):
        # Enable-only: a falsy env value is NOT consulted, so it cannot override an enabling
        # settings.json below. Returning early on truthy is what makes the precedence asymmetric.
        return True
    try:
        workflow_settings = _load().get("workflow", {})
        if not isinstance(workflow_settings, dict):
            return False
        return bool(workflow_settings.get("require_approval", False))
    except Exception as e:
        logger.warning(
            "Failed to read workflow.require_approval, defaulting to False (gate disabled): %s", e
        )
        return False


def is_instruction_promotion_enabled() -> bool:
    """Return True when learned-lesson promotion into profile files is enabled.

    Precedence: CAO_MEMORY_INSTRUCTION_PROMOTION_ENABLED env var >
    memory.instruction_promotion_enabled in settings.json > default (False).

    Promotion is the highest-risk learning tier — it mutates agent profile
    markdown shared by every session — so it nests inside learning:
    promotion ⊂ learning ⊂ memory. Any parent off forces it off. Read
    errors default to False (fail closed).
    """
    try:
        if not is_learning_enabled():
            return False
        settings = get_memory_settings()
        return bool(settings.get("instruction_promotion_enabled", False))
    except Exception as e:
        logger.warning(
            f"Failed to read memory.instruction_promotion_enabled, defaulting to False: {e}"
        )
        return False


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


# Workflow-journal retention/capture settings (issue #504, U7). Namespaced under
# the "memory" block like ``audit_log_day_cap_bytes``; written through
# ``set_memory_setting`` and read via ``get_memory_settings().get(<key>, <default>)``.
# ``workflow_journal_capture_output`` is a bool gate (default False, NFR-SEC-2); the
# other three are non-negative ints (byte cap / age / count) validated on write.
_WORKFLOW_JOURNAL_BOOL_KEYS = frozenset({"workflow_journal_capture_output"})
_WORKFLOW_JOURNAL_INT_KEYS = frozenset(
    {
        "workflow_journal_output_cap_bytes",
        "workflow_journal_retention_days",
        "workflow_journal_retention_count",
    }
)


def set_memory_setting(key: str, value: Any) -> Dict[str, Any]:
    """Update a single memory setting.

    Supported keys:
        ``enabled`` (bool) — master switch for the memory subsystem.
        ``flush_threshold`` (float, 0.0 < x ≤ 1.0) — context-usage trigger.
        ``lint_enabled`` (bool) — expensive wiki lint enrichment switch.
        ``learning_enabled`` (bool) — workflow self-learning (outcome capture).
        ``instruction_promotion_enabled`` (bool) — learned-lesson promotion
        into agent profile files (requires learning_enabled).
        ``workflow_journal_capture_output`` (bool) — opt-in workflow output capture
            (issue #504, U7; default False, NFR-SEC-2).
        ``workflow_journal_output_cap_bytes`` (int > 0) — per-output byte cap when
            capture is enabled (U7; default 8192, NFR-SEC-4).
        ``workflow_journal_retention_days`` (int ≥ 0) — run-age retention bound
            (U7; default 30, NFR-SEC-3).
        ``workflow_journal_retention_count`` (int ≥ 0) — most-recent run-count
            retention bound (U7; default 100, NFR-SEC-3).
    """
    settings = _load()
    memory = settings.get("memory", {})
    if not isinstance(memory, dict):
        memory = {}

    if key == "enabled":
        if not isinstance(value, bool):
            raise ValueError(f"enabled must be a bool, got {type(value).__name__}")
        memory[key] = value
    elif key == "lint_enabled":
        if not isinstance(value, bool):
            raise ValueError(f"lint_enabled must be a bool, got {type(value).__name__}")
        memory[key] = value
    elif key == "learning_enabled":
        if not isinstance(value, bool):
            raise ValueError(f"learning_enabled must be a bool, got {type(value).__name__}")
        memory[key] = value
    elif key == "instruction_promotion_enabled":
        if not isinstance(value, bool):
            raise ValueError(
                f"instruction_promotion_enabled must be a bool, got {type(value).__name__}"
            )
        memory[key] = value
    elif key == "flush_threshold":
        fval = float(value)
        if not (0.0 < fval <= 1.0):
            raise ValueError(f"flush_threshold must be between 0.0 and 1.0, got {fval}")
        memory[key] = fval
    elif key in _WORKFLOW_JOURNAL_BOOL_KEYS:
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be a bool, got {type(value).__name__}")
        memory[key] = value
    elif key in _WORKFLOW_JOURNAL_INT_KEYS:
        # bool is an int subclass — reject it so True/False can't masquerade as 1/0.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{key} must be an int, got {type(value).__name__}")
        min_value = 1 if key == "workflow_journal_output_cap_bytes" else 0
        if value < min_value:
            raise ValueError(f"{key} must be >= {min_value}, got {value}")
        memory[key] = value
    else:
        raise ValueError(f"Unknown memory setting: {key}")

    settings["memory"] = memory
    _save(settings)
    logger.info(f"Updated memory setting: {key}={memory[key]}")
    return get_memory_settings()


def get_extra_agent_dirs() -> List[str]:
    """Get extra agent scan directories (user-added custom paths).

    Reads from the nested schema first (``agents.extra_dirs``), falls back to
    the legacy flat key (``extra_agent_dirs``) for backward compatibility.
    """
    settings = _load()
    # Nested format: {"agents": {"extra_dirs": [...]}}
    nested = settings.get("agents", {})
    if (
        isinstance(nested, dict)
        and "extra_dirs" in nested
        and isinstance(nested["extra_dirs"], list)
    ):
        dirs = nested["extra_dirs"]
    else:
        # Legacy flat format: {"extra_agent_dirs": [...]}
        dirs = settings.get("extra_agent_dirs", [])
    return dirs if isinstance(dirs, list) else []


def set_extra_agent_dirs(dirs: List[str]) -> List[str]:
    """Set extra agent scan directories.

    Writes to nested schema (``agents.extra_dirs``) and legacy flat key
    (``extra_agent_dirs``) for backward compatibility.
    """
    settings = _load()
    extra_agent_dirs = [d for d in dirs if d.strip()]
    # Write nested format
    agents_section = settings.get("agents", {})
    if not isinstance(agents_section, dict):
        agents_section = {}
    agents_section["extra_dirs"] = extra_agent_dirs
    settings["agents"] = agents_section
    # Also write flat key for backward compat
    settings["extra_agent_dirs"] = extra_agent_dirs
    _save(settings)
    # Prune disabled entries that no longer point at any configured directory —
    # otherwise removing an extra dir leaves a stale disabled entry behind, and
    # re-adding that path later would come back silently pre-disabled.
    disabled = get_disabled_agent_dirs()
    if disabled:
        set_disabled_agent_dirs(disabled)
    return extra_agent_dirs


def get_extra_skill_dirs() -> List[str]:
    """Get extra skill scan directories (user-added custom paths).

    Reads from the nested schema first (``skills.extra_dirs``), falls back to
    the legacy flat key (``extra_skill_dirs``) for backward compatibility.

    Filters to non-empty strings so malformed persisted data (e.g. a manually
    edited ``settings.json`` storing ``null`` or numbers) cannot later raise a
    ``TypeError`` from ``Path(extra)`` and break skill listing/loading.
    """
    settings = _load()
    # Nested format: {"skills": {"extra_dirs": [...]}}
    nested = settings.get("skills", {})
    if (
        isinstance(nested, dict)
        and "extra_dirs" in nested
        and isinstance(nested["extra_dirs"], list)
    ):
        dirs = nested["extra_dirs"]
    else:
        # Legacy flat format: {"extra_skill_dirs": [...]}
        dirs = settings.get("extra_skill_dirs", [])
    if not isinstance(dirs, list):
        return []
    return [d.strip() for d in dirs if isinstance(d, str) and d.strip()]


def set_extra_skill_dirs(dirs: List[str]) -> List[str]:
    """Set extra skill scan directories.

    Writes to nested schema (``skills.extra_dirs``) and legacy flat key
    (``extra_skill_dirs``) for backward compatibility.
    """
    settings = _load()
    extra_skill_dirs = [d.strip() for d in dirs if isinstance(d, str) and d.strip()]
    # Write nested format
    skills_section = settings.get("skills", {})
    if not isinstance(skills_section, dict):
        skills_section = {}
    skills_section["extra_dirs"] = extra_skill_dirs
    settings["skills"] = skills_section
    # Also write flat key for backward compat
    settings["extra_skill_dirs"] = extra_skill_dirs
    _save(settings)
    return extra_skill_dirs
