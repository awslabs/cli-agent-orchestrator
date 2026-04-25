"""Helpers for managing CAO environment variables."""

from string import Template

from dotenv import dotenv_values, set_key, unset_key

from cli_agent_orchestrator.constants import CAO_ENV_FILE


def load_env_vars() -> dict[str, str]:
    """Load managed environment variables from the CAO .env file."""
    if not CAO_ENV_FILE.exists():
        return {}

    env_values = dotenv_values(CAO_ENV_FILE)
    return {key: value for key, value in env_values.items() if value is not None}


def resolve_env_vars(raw_text: str) -> str:
    """Resolve ``${VAR}`` placeholders from the managed CAO .env file."""
    return Template(raw_text).safe_substitute(load_env_vars())


def set_env_var(key: str, value: str) -> None:
    """Create or update a managed environment variable."""
    CAO_ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not CAO_ENV_FILE.exists():
        CAO_ENV_FILE.touch(mode=0o600, exist_ok=True)
    set_key(str(CAO_ENV_FILE), key, value)


def unset_env_var(key: str) -> None:
    """Remove a managed environment variable if the env file exists."""
    if not CAO_ENV_FILE.exists():
        return
    unset_key(str(CAO_ENV_FILE), key)


def list_env_vars() -> dict[str, str]:
    """Return the current managed environment variables."""
    return load_env_vars()
