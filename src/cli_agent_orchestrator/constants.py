"""Constants for CLI Agent Orchestrator application."""

from pathlib import Path

# Session configuration
SESSION_PREFIX = "cao-"

# Available providers
PROVIDERS = ['q_cli', 'claude_code']
DEFAULT_PROVIDER = "q_cli"

# Tmux capture limits
TMUX_HISTORY_LINES = 200

# TODO: remove the terminal history lines and status check lines if they aren't used anywhere
# Terminal output capture limits
TERMINAL_HISTORY_LINES = 200
STATUS_CHECK_LINES = 100

# Application directories
CAO_HOME_DIR = Path.home() / ".aws" / "cli-agent-orchestrator"
DB_DIR = CAO_HOME_DIR / "db"
LOG_DIR = CAO_HOME_DIR / "logs"
TERMINAL_LOG_DIR = LOG_DIR / "terminal"
AGENT_CONTEXT_DIR = CAO_HOME_DIR / "agent-context"

# Q CLI directories
Q_AGENTS_DIR = Path.home() / ".aws" / "amazonq" / "cli-agents"

# Database configuration
DATABASE_FILE = DB_DIR / "cli-agent-orchestrator.db"
DATABASE_URL = f"sqlite:///{DATABASE_FILE}"

# Server configuration
SERVER_HOST = "localhost"
SERVER_PORT = 8080
SERVER_VERSION = "0.1.0"
CORS_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]
