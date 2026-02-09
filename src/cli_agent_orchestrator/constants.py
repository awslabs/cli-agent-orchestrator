"""Constants for CLI Agent Orchestrator (CAO) application.

This module defines all configuration constants used throughout the CAO application,
including directory paths, server settings, and provider configurations.

The CAO application orchestrates multiple CLI-based AI agents (Kiro CLI, Claude Code,
Codex, Kimi CLI, Gemini CLI, Q CLI) through tmux sessions, providing a unified interface
for agent management.
"""

from pathlib import Path

from cli_agent_orchestrator.models.provider import ProviderType

# =============================================================================
# Session Configuration
# =============================================================================
# All CAO-managed tmux sessions are prefixed to distinguish them from user sessions
SESSION_PREFIX = "cao-"

# =============================================================================
# Provider Configuration
# =============================================================================
# Available CLI providers - derived from the ProviderType enum for consistency
PROVIDERS = [p.value for p in ProviderType]

# Default provider used when --provider flag is not specified
# Kiro CLI is the recommended provider for new projects
DEFAULT_PROVIDER = ProviderType.KIRO_CLI.value

# =============================================================================
# Tmux Configuration
# =============================================================================
# Maximum lines of terminal history to capture when analyzing output
# Higher values provide more context but increase memory usage
TMUX_HISTORY_LINES = 200

# =============================================================================
# Application Directory Structure
# =============================================================================
# Base directory for all CAO data (~/.aws/cli-agent-orchestrator)
CAO_HOME_DIR = Path.home() / ".aws" / "cli-agent-orchestrator"

# SQLite database directory
DB_DIR = CAO_HOME_DIR / "db"

# Log file directory structure
LOG_DIR = CAO_HOME_DIR / "logs"
TERMINAL_LOG_DIR = LOG_DIR / "terminal"  # Per-terminal log files for pipe-pane output
TERMINAL_LOG_DIR.mkdir(parents=True, exist_ok=True)

# =============================================================================
# Inbox Service Configuration
# =============================================================================
# Polling interval for detecting log file changes (seconds)
# Lower values = faster response, higher CPU usage
INBOX_POLLING_INTERVAL = 5

# =============================================================================
# Cleanup Service Configuration
# =============================================================================
# Data retention period for terminals, messages, and log files
RETENTION_DAYS = 14

# =============================================================================
# Agent Profile Storage
# =============================================================================
# Directory for agent context files (shared state between sessions)
AGENT_CONTEXT_DIR = CAO_HOME_DIR / "agent-context"

# Local agent store for custom agent profiles
LOCAL_AGENT_STORE_DIR = CAO_HOME_DIR / "agent-store"

# Provider-specific agent directories
Q_AGENTS_DIR = Path.home() / ".aws" / "amazonq" / "cli-agents"  # Q CLI agents
KIRO_AGENTS_DIR = Path.home() / ".kiro" / "agents"  # Kiro CLI agents

# =============================================================================
# Database Configuration
# =============================================================================
# SQLite database file path and connection URL
DATABASE_FILE = DB_DIR / "cli-agent-orchestrator.db"
DATABASE_URL = f"sqlite:///{DATABASE_FILE}"

# =============================================================================
# Server Configuration
# =============================================================================
# FastAPI server settings for the CAO API
SERVER_HOST = "localhost"
SERVER_PORT = 9889
SERVER_VERSION = "0.1.0"
API_BASE_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"

# CORS allowed origins for web-based clients
CORS_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]
