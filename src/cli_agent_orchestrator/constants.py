"""Constants for CLI Agent Orchestrator application."""

from pathlib import Path

from cli_agent_orchestrator.models.provider import ProviderType

# Session configuration
SESSION_PREFIX = "cao-"

# Available providers (derived from enum)
PROVIDERS = [p.value for p in ProviderType]
DEFAULT_PROVIDER = ProviderType.Q_CLI.value

# Tmux capture limits
TMUX_HISTORY_LINES = 200

# Application directories
CAO_HOME_DIR = Path.home() / ".aws" / "cli-agent-orchestrator"
DB_DIR = CAO_HOME_DIR / "db"
LOG_DIR = CAO_HOME_DIR / "logs"
TERMINAL_LOG_DIR = LOG_DIR / "terminal"
TERMINAL_LOG_DIR.mkdir(parents=True, exist_ok=True)

# Terminal log configuration
INBOX_POLLING_INTERVAL = 5  # Seconds between polling for log file changes
INBOX_SERVICE_TAIL_LINES = 5  # Number of lines to check in get_status for inbox service

# Cleanup configuration
RETENTION_DAYS = 14  # Days to keep terminals, messages, and logs

AGENT_CONTEXT_DIR = CAO_HOME_DIR / "agent-context"

# Agent store directories
LOCAL_AGENT_STORE_DIR = CAO_HOME_DIR / "agent-store"

# Q CLI directories
Q_AGENTS_DIR = Path.home() / ".aws" / "amazonq" / "cli-agents"

# Kiro CLI directories
KIRO_AGENTS_DIR = Path.home() / ".kiro" / "agents"

# Database configuration
DATABASE_FILE = DB_DIR / "cli-agent-orchestrator.db"
DATABASE_URL = f"sqlite:///{DATABASE_FILE}"

# Server configuration
SERVER_HOST = "localhost"
SERVER_PORT = 9889
SERVER_VERSION = "0.1.0"
API_BASE_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"
CORS_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]
