"""ConfigService — single reader for CAO's unified configuration.

Unifies the two config surfaces named in issue #357:

- ``~/.aws/cli-agent-orchestrator/settings.json`` (the ``agent_dirs``,
  ``extra_agent_dirs``, ``extra_skill_dirs``, ``server``, ``memory`` sections
  already served by :mod:`services.settings_service`)
- ``~/.aws/cli-agent-orchestrator/config.json`` (``terminal_backend`` /
  ``herdr_session``, previously read inline by ``backends/factory.py``)

into one ``settings.json`` file plus a single ``CAO_*`` env-var registry,
resolved through one precedence chain everywhere:

    CLI flag > CAO_* env var > config file > built-in default

``ConfigService.get()`` is the front door. For sections already backed by
tested logic in ``settings_service`` (agents, skills, server, memory) it
delegates there so existing validation/clamping behavior is preserved
byte-for-byte. For the sections that had no home before this issue
(terminal, apps, auth, network, logging) it owns the file section directly
under the *nested* schema described in issue #357.

``.env`` handling (``utils/env.py``) is out of scope — untouched.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from cli_agent_orchestrator.constants import CAO_HOME_DIR

logger = logging.getLogger(__name__)


# =============================================================================
# Schema — the typed shape of the unified config (issue #357). Each section
# below is a Pydantic model so the schema is self-documenting and validated;
# ``get_config()`` assembles one from resolved values (env > file > default).
# =============================================================================


class AgentsConfig(BaseModel):
    dirs: Dict[str, str] = Field(default_factory=dict)
    extra_dirs: List[str] = Field(default_factory=list)
    roles: Dict[str, List[str]] = Field(default_factory=dict)


class SkillsConfig(BaseModel):
    extra_dirs: List[str] = Field(default_factory=list)


class ServerConfig(BaseModel):
    mcp_request_timeout: int = 30
    event_bus_max_queue_size: int = 1024
    provider_init_timeout: int = 60
    startup_prompt_handler_timeout: int = 20


class MemoryConfig(BaseModel):
    enabled: bool = True
    compile_mode: str = "llm"
    flush_threshold: float = 0.85
    compile_timeout_s: float = 120.0


class TerminalConfig(BaseModel):
    backend: str = "tmux"
    herdr_session: str = "cao"


class AppsConfig(BaseModel):
    enabled: bool = False
    static_dir: Optional[str] = None


class NetworkConfig(BaseModel):
    """env-var only; settings.json values are not yet honored.

    ``constants.py`` builds ``CORS_ORIGINS``/``ALLOWED_HOSTS``/``WS_ALLOWED_CLIENTS``
    as module-level lists at import time and Starlette's CORS/TrustedHost
    middleware are instantiated once, holding a reference to those exact list
    objects (``add_local_cors_origins`` relies on this — see constants.py).
    Rewiring them through ConfigService would require either mutating those
    lists after settings.json changes (no invalidation mechanism exists yet)
    or restructuring the middleware wiring — out of scope for this PR. Only
    the ``CAO_ALLOWED_HOSTS``/``CAO_CORS_ORIGINS``/``CAO_WS_ALLOWED_CLIENTS``
    env vars are read (in ``constants.py``, not through this schema).
    """

    allowed_hosts: List[str] = Field(default_factory=list)
    cors_origins: List[str] = Field(default_factory=list)
    ws_allowed_clients: List[str] = Field(default_factory=list)


class AuthConfig(BaseModel):
    """env-var only; settings.json values are not yet honored.

    ``security/auth.py`` is the actual authentication *enforcement* boundary
    (not a UX gate) and is kept on direct ``os.getenv`` reads to avoid
    changing security-critical resolution behavior in this PR. See the
    "Config note" in that module's docstring.
    """

    jwks_uri: str = ""
    audience: str = ""
    issuer: str = ""


class LoggingConfig(BaseModel):
    level: str = "INFO"


class CAOConfig(BaseModel):
    """The full unified schema. See issue #357."""

    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    terminal: TerminalConfig = Field(default_factory=TerminalConfig)
    apps: AppsConfig = Field(default_factory=AppsConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


# Deprecated second surface. Read once for migration, then ignored.
LEGACY_CONFIG_FILE = CAO_HOME_DIR / "config.json"

# Dotted schema path -> on-disk key path, for the two sections whose
# settings_service-managed keys predate the nested "agents"/"skills"
# schema and are stored flat. Every other section (server, memory,
# terminal, apps, auth, network, logging) nests 1:1 with its dotted path.
_LEGACY_KEY_MAP: Dict[str, Tuple[str, ...]] = {
    "agents.dirs": ("agent_dirs",),
    "agents.extra_dirs": ("extra_agent_dirs",),
    "agents.roles": ("roles",),
    "skills.extra_dirs": ("extra_skill_dirs",),
}

# Built-in defaults for the sections this module owns directly (terminal,
# apps, auth, network, logging). Agents/skills/server/memory defaults live in
# settings_service, which this module delegates to for those sections.
_OWNED_DEFAULTS: Dict[str, Any] = {
    "terminal.backend": "tmux",
    "terminal.herdr_session": "cao",
    "apps.enabled": False,
    "apps.static_dir": None,
    "auth.jwks_uri": "",
    "auth.audience": "",
    "auth.issuer": "",
    "logging.level": "INFO",
    "network.allowed_hosts": [],
    "network.cors_origins": [],
    "network.ws_allowed_clients": [],
}

# Env-var registry: every CAO_* var this schema recognizes, mapped to its
# config path, value type, and default. Backs both ``get()``'s env-precedence
# tier and the ``cao config list`` introspection view. Types: "str", "bool",
# "int", "float", "list" (comma-separated).
ENV_REGISTRY: Dict[str, Tuple[str, str, Any]] = {
    "CAO_TERMINAL_BACKEND": ("terminal.backend", "str", "tmux"),
    "CAO_HERDR_SESSION": ("terminal.herdr_session", "str", "cao"),
    "CAO_MCP_APPS_ENABLED": ("apps.enabled", "bool", False),
    "CAO_MCP_APPS_STATIC_DIR": ("apps.static_dir", "str", None),
    "CAO_AUTH_JWKS_URI": ("auth.jwks_uri", "str", ""),
    "CAO_AUTH_AUDIENCE": ("auth.audience", "str", ""),
    "CAO_AUTH_ISSUER": ("auth.issuer", "str", ""),
    "CAO_LOG_LEVEL": ("logging.level", "str", "INFO"),
    "CAO_ALLOWED_HOSTS": ("network.allowed_hosts", "list", []),
    "CAO_CORS_ORIGINS": ("network.cors_origins", "list", []),
    "CAO_WS_ALLOWED_CLIENTS": ("network.ws_allowed_clients", "list", []),
    "CAO_MEMORY_ENABLED": ("memory.enabled", "bool", True),
    "CAO_MEMORY_COMPILE_MODE": ("memory.compile_mode", "str", "llm"),
    "CAO_MEMORY_FLUSH_THRESHOLD": ("memory.flush_threshold", "float", 0.85),
    "CAO_MCP_REQUEST_TIMEOUT": ("server.mcp_request_timeout", "int", 30),
    "CAO_EVENT_BUS_MAX_QUEUE_SIZE": ("server.event_bus_max_queue_size", "int", 1024),
    "CAO_PROVIDER_INIT_TIMEOUT": ("server.provider_init_timeout", "int", 60),
    "CAO_STARTUP_PROMPT_HANDLER_TIMEOUT": (
        "server.startup_prompt_handler_timeout",
        "int",
        20,
    ),
}

# Reverse index: dotted path -> env var name, for get()'s env-precedence lookup.
_PATH_TO_ENV: Dict[str, str] = {path: env for env, (path, _, _) in ENV_REGISTRY.items()}

_migration_logged = False


def _coerce_env_value(raw: str, kind: str) -> Any:
    if kind == "bool":
        return raw.strip().lower() in ("1", "true", "yes")
    if kind == "int":
        return int(raw)
    if kind == "float":
        return float(raw)
    if kind == "list":
        return [item.strip() for item in raw.split(",") if item.strip()]
    return raw


def _get_nested(data: Dict[str, Any], keys: Tuple[str, ...]) -> Any:
    node: Any = data
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _set_nested(data: Dict[str, Any], keys: Tuple[str, ...], value: Any) -> None:
    node = data
    for key in keys[:-1]:
        node = node.setdefault(key, {})
    node[keys[-1]] = value


def _settings_file() -> Path:
    """Return the unified settings file path.

    Delegates to ``settings_service.SETTINGS_FILE`` (rather than duplicating
    the constant) so both modules always agree on one path, including in
    tests that patch ``settings_service.SETTINGS_FILE``.
    """
    from cli_agent_orchestrator.services import settings_service

    return settings_service.SETTINGS_FILE


def _load_raw() -> Dict[str, Any]:
    """Load the unified settings file, migrating legacy config.json in place.

    On first run after upgrading, if a legacy ``config.json`` exists and the
    unified file has no ``terminal`` section yet, its ``terminal_backend`` /
    ``herdr_session`` keys are copied into ``settings.json`` under
    ``terminal`` and the move is logged. ``config.json`` itself is left on
    disk (untouched) but is no longer read once migrated.

    This is a plain read-modify-write with no file lock: it assumes a single
    CAO process touches ``settings.json`` at a time (the same assumption
    ``settings_service._load``/``_save`` already make). A concurrent ``set()``
    from a second process during migration could be lost. Acceptable for a
    single-operator local tool; would need a lock (e.g. ``filelock``) if CAO
    ever supports multiple concurrent writers to the same settings file.
    """
    global _migration_logged

    settings_file = _settings_file()
    data: Dict[str, Any] = {}
    if settings_file.exists():
        try:
            loaded = json.loads(settings_file.read_text())
            if isinstance(loaded, dict):
                data = loaded
        except Exception as e:
            logger.warning(f"Failed to read {settings_file}: {e}")

    if "terminal" not in data and LEGACY_CONFIG_FILE.exists():
        try:
            legacy = json.loads(LEGACY_CONFIG_FILE.read_text())
        except Exception as e:
            logger.warning(f"Failed to read legacy {LEGACY_CONFIG_FILE}: {e}")
            legacy = {}
        if isinstance(legacy, dict) and legacy:
            terminal_section = {}
            if "terminal_backend" in legacy:
                terminal_section["backend"] = legacy["terminal_backend"]
            if "herdr_session" in legacy:
                terminal_section["herdr_session"] = legacy["herdr_session"]
            if terminal_section:
                data["terminal"] = terminal_section
                _save_raw(data)
                if not _migration_logged:
                    logger.info(
                        f"Migrated legacy {LEGACY_CONFIG_FILE} into {settings_file} "
                        "under the 'terminal' key. config.json is deprecated; "
                        "this migration runs once."
                    )
                    _migration_logged = True
    return data


def _save_raw(data: Dict[str, Any]) -> None:
    settings_file = _settings_file()
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(json.dumps(data, indent=2))


def _get_from_file(path: str) -> Any:
    """Resolve a dotted path from the unified file, honoring legacy flat keys."""
    data = _load_raw()
    keys = _LEGACY_KEY_MAP.get(path)
    if keys is not None:
        return _get_nested(data, keys)
    return _get_nested(data, tuple(path.split(".")))


def _get_owned_section(path: str, default: Any) -> Any:
    """Delegate reads for agents/skills/server/memory to settings_service."""
    from cli_agent_orchestrator.services import settings_service

    section, _, key = path.partition(".")
    if path == "agents.dirs":
        return settings_service.get_agent_dirs()
    if path == "agents.extra_dirs":
        return settings_service.get_extra_agent_dirs()
    if path == "agents.roles":
        data = settings_service._load()
        # Nested format: {"agents": {"roles": {...}}}
        nested = data.get("agents", {})
        if isinstance(nested, dict) and "roles" in nested and isinstance(nested["roles"], dict):
            return nested["roles"]
        # Legacy flat format: {"roles": {...}}
        return data.get("roles", {})
    if path == "skills.extra_dirs":
        return settings_service.get_extra_skill_dirs()
    if section == "server":
        return settings_service.get_server_settings().get(key, default)
    if section == "memory":
        if key == "enabled":
            return settings_service.is_memory_enabled()
        if key == "compile_mode":
            return settings_service.get_compile_mode()
        if key == "compile_timeout_s":
            return settings_service.get_compile_timeout_s()
        return settings_service.get_memory_settings().get(key, default)
    raise KeyError(path)


_OWNED_SECTIONS = frozenset({"agents", "skills", "server", "memory"})


def _get_value(path: str, default: Any = None, override: Optional[Any] = None) -> Any:
    """Resolve a config value: CLI-flag override > env var > file > default.

    ``path`` is a dotted schema path, e.g. ``"terminal.backend"`` or
    ``"apps.enabled"``. ``override`` represents an explicit CLI-flag value
    from the caller (e.g. ``cao-server --terminal herdr``) and, when not
    ``None``, always wins.
    """
    if override is not None:
        return override

    env_name = _PATH_TO_ENV.get(path)
    if env_name is not None:
        import os

        raw = os.environ.get(env_name)
        if raw is not None and raw != "":
            _, kind, _ = ENV_REGISTRY[env_name]
            try:
                return _coerce_env_value(raw, kind)
            except ValueError:
                logger.warning(
                    f"Ignoring invalid {env_name}={raw!r} for {path}; using file/default"
                )

    section = path.split(".", 1)[0]
    if section in _OWNED_SECTIONS:
        try:
            value = _get_owned_section(path, default)
        except KeyError:
            value = None
        return value if value is not None else default

    file_value = _get_from_file(path)
    if file_value is not None:
        return file_value
    return _OWNED_DEFAULTS.get(path, default)


def _set_value(path: str, value: Any) -> Any:
    """Persist a config value under its dotted schema path.

    Agents/skills sections route through settings_service's existing
    setters (preserving their validation); all other sections write
    directly into the unified file's nested structure.
    """
    from cli_agent_orchestrator.services import settings_service

    if path == "agents.extra_dirs":
        return settings_service.set_extra_agent_dirs(value)
    if path == "skills.extra_dirs":
        return settings_service.set_extra_skill_dirs(value)
    if path.startswith("agents.dirs."):
        provider = path.split(".", 2)[2]
        return settings_service.set_agent_dirs({provider: value})
    if path == "agents.roles" or path.startswith("agents.roles."):
        # Write both nested and flat for backward compat
        data = _load_raw()
        if path == "agents.roles":
            roles = value
        else:
            role_name = path.split(".", 2)[2]
            roles = data.get("roles", {})
            if not isinstance(roles, dict):
                roles = {}
            roles[role_name] = value
        # Nested format
        agents_section = data.get("agents", {})
        if not isinstance(agents_section, dict):
            agents_section = {}
        agents_section["roles"] = roles
        data["agents"] = agents_section
        # Flat format
        data["roles"] = roles
        _save_raw(data)
        return roles
    if path.startswith("memory."):
        key = path.split(".", 1)[1]
        return settings_service.set_memory_setting(key, value)

    keys = _LEGACY_KEY_MAP.get(path, tuple(path.split(".")))
    data = _load_raw()
    _set_nested(data, keys, value)
    _save_raw(data)
    return value


_ALL_PATHS = sorted(
    set(_PATH_TO_ENV.keys())
    | {
        "agents.dirs",
        "agents.extra_dirs",
        "agents.roles",
        "skills.extra_dirs",
        "server.mcp_request_timeout",
        "server.event_bus_max_queue_size",
        "server.provider_init_timeout",
        "server.startup_prompt_handler_timeout",
        "memory.enabled",
        "memory.compile_mode",
        "memory.flush_threshold",
        "memory.compile_timeout_s",
    }
)


class ConfigService:
    """Single reader/writer for CAO's unified configuration.

    Stateless — every method re-resolves from env/file on each call (settings
    files are small and infrequently read; see ``settings_service.get_server_settings``
    for the one hot-path that still caches on mtime).
    """

    @staticmethod
    def get(path: str, default: Any = None, override: Optional[Any] = None) -> Any:
        """Resolve ``path`` via CLI override > CAO_* env var > file > default."""
        return _get_value(path, default=default, override=override)

    @staticmethod
    def set(path: str, value: Any) -> Any:
        """Persist ``value`` at dotted schema ``path`` in the unified settings file."""
        return _set_value(path, value)

    @staticmethod
    def path() -> Path:
        """Return the absolute path to the unified settings file."""
        return _settings_file()

    @staticmethod
    def list_all() -> Dict[str, Any]:
        """Return every known config path resolved to its effective value.

        Reflects the same precedence ``get()`` uses (env beats file beats
        default). Intended for ``cao config list`` and debugging.
        """
        return {p: _get_value(p) for p in _ALL_PATHS}

    @staticmethod
    def get_config() -> CAOConfig:
        """Assemble and validate the full typed config from resolved values."""
        return CAOConfig(
            agents=AgentsConfig(
                dirs=_get_value("agents.dirs", default={}),
                extra_dirs=_get_value("agents.extra_dirs", default=[]),
                roles=_get_value("agents.roles", default={}),
            ),
            skills=SkillsConfig(extra_dirs=_get_value("skills.extra_dirs", default=[])),
            server=ServerConfig(
                mcp_request_timeout=_get_value("server.mcp_request_timeout", default=30),
                event_bus_max_queue_size=_get_value(
                    "server.event_bus_max_queue_size", default=1024
                ),
                provider_init_timeout=_get_value("server.provider_init_timeout", default=60),
                startup_prompt_handler_timeout=_get_value(
                    "server.startup_prompt_handler_timeout", default=20
                ),
            ),
            memory=MemoryConfig(
                enabled=_get_value("memory.enabled", default=True),
                compile_mode=_get_value("memory.compile_mode", default="llm"),
                flush_threshold=_get_value("memory.flush_threshold", default=0.85),
                compile_timeout_s=_get_value("memory.compile_timeout_s", default=120.0),
            ),
            terminal=TerminalConfig(
                backend=_get_value("terminal.backend", default="tmux"),
                herdr_session=_get_value("terminal.herdr_session", default="cao"),
            ),
            apps=AppsConfig(
                enabled=_get_value("apps.enabled", default=False),
                static_dir=_get_value("apps.static_dir", default=None),
            ),
            network=NetworkConfig(
                allowed_hosts=_get_value("network.allowed_hosts", default=[]),
                cors_origins=_get_value("network.cors_origins", default=[]),
                ws_allowed_clients=_get_value("network.ws_allowed_clients", default=[]),
            ),
            auth=AuthConfig(
                jwks_uri=_get_value("auth.jwks_uri", default=""),
                audience=_get_value("auth.audience", default=""),
                issuer=_get_value("auth.issuer", default=""),
            ),
            logging=LoggingConfig(level=_get_value("logging.level", default="INFO")),
        )
