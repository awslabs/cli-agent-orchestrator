# CLI Agent Orchestrator Codebase

## Architecture Overview

```
CLI/MCP Server → API → Services → Clients/Providers → Tmux/Database
```

## Directory Structure

```
src/cli_agent_orchestrator/
├── cli/commands/          # Entry Point: CLI commands
│   ├── launch.py          # Creates terminals with agent profiles
│   └── init.py            # Initializes database
├── mcp_server/            # Entry Point: MCP server
│   └── server.py          # Handoff tool (reads CAO_TERMINAL_ID)
├── api/                   # Entry Point: HTTP API
│   └── main.py            # FastAPI endpoints
├── services/              # Service Layer: Business logic
│   ├── session_service.py # List, get, delete sessions
│   └── terminal_service.py# Create, get, send input, get output, delete terminals
├── clients/               # Client Layer: External systems
│   ├── tmux.py            # Tmux operations (sets CAO_TERMINAL_ID)
│   └── database.py        # SQLite with terminals table
├── providers/             # Provider Layer: CLI tool integration
│   ├── base.py            # Abstract provider interface
│   ├── manager.py         # Maps terminal_id → provider
│   ├── q_cli.py           # Q CLI provider
│   └── claude_code.py     # Claude Code provider
├── models/                # Data models
│   └── terminal.py        # Terminal, TerminalStatus
├── utils/                 # Utilities
│   ├── terminal.py        # Generate IDs, wait for shell/status
│   └── logging.py         # File-based logging
├── agent_store/           # Agent profile definitions
└── constants.py           # Application constants
```

## Data Flow Example

```
cao launch --agents code_sup
  ↓
terminal_service.create_terminal()
  ↓
tmux_client.create_session(terminal_id)  # Sets CAO_TERMINAL_ID
  ↓
database.create_terminal()
  ↓
provider_manager.create_provider()
  ↓
provider.initialize()  # Waits for shell, sends command, waits for IDLE
  ↓
Returns Terminal model
```
