# CLI Agent Orchestrator Codebase

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Entry Points                                 │
├─────────────────────────────┬───────────────────────────────────────┤
│       CLI Commands          │         MCP Server                    │
│       (cao launch)          │    (handoff, send_message)            │
└──────────────┬──────────────┴──────────────┬────────────────────────┘
               │                             │
               └─────────────┬───────────────┘
                             │
                      ┌──────▼──────┐
                      │  FastAPI    │
                      │  HTTP API   │
                      │  (:9889)    │
                      └──────┬──────┘
                             │
                      ┌──────▼──────┐
                      │  Services   │
                      │  Layer      │
                      ├─────────────┤
                      │ • session   │
                      │ • terminal  │
                      │ • inbox     │
                      │ • flow      │
                      └──────┬──────┘
                             │
                ┌────────────┴────────────┐
                │                         │
           ┌────▼────┐               ┌────▼─────┐
           │ Clients │               │Providers │
           ├─────────┤               ├──────────┤
           │ • tmux  │               │ • q_cli  │
           │ • db    │               │ • kiro   │
           └────┬────┘               │   _cli   │
                │                    │ • claude │
         ┌──────┴──────┐             │   _code  │
         │             │             └────┬─────┘
    ┌────▼────┐  ┌─────▼─────┐          │
    │  Tmux   │  │  SQLite   │     ┌─────▼──────┐
    │ Sessions│  │  Database │     │ CLI Tools  │
    └─────────┘  └───────────┘     │• Amazon Q  │
                                   │  CLI       │
                                   │• Kiro CLI  │
                                   │• Claude    │
                                   │  Code      │
                                   └────────────┘
```

## Directory Structure

```
src/cli_agent_orchestrator/
├── cli/commands/          # Entry Point: CLI commands
│   ├── launch.py          # Creates terminals with agent profiles
│   └── init.py            # Initializes database
├── mcp_server/            # Entry Point: MCP server
│   ├── server.py          # Handoff & send_message tools
│   └── models.py          # HandoffResult model
├── api/                   # Entry Point: HTTP API
│   └── main.py            # FastAPI endpoints (port 9889)
├── services/              # Service Layer: Business logic
│   ├── session_service.py # List, get, delete sessions
│   ├── terminal_service.py# Create, get, send input, get output, delete terminals
│   ├── inbox_service.py   # Terminal-to-terminal messaging with watchdog
│   └── flow_service.py    # Scheduled flow execution
├── clients/               # Client Layer: External systems
│   ├── tmux.py            # Tmux operations (sets CAO_TERMINAL_ID)
│   └── database.py        # SQLite with terminals & inbox_messages tables
├── providers/             # Provider Layer: CLI tool integration
│   ├── base.py            # Abstract provider interface
│   ├── manager.py         # Maps terminal_id → provider
│   ├── q_cli.py           # Amazon Q CLI provider (q_cli)
│   ├── kiro_cli.py        # Kiro CLI provider (kiro_cli)
│   └── claude_code.py     # Claude Code provider (claude_code)
├── models/                # Data models
│   ├── terminal.py        # Terminal, TerminalStatus
│   ├── session.py         # Session model
│   ├── inbox.py           # InboxMessage, MessageStatus
│   ├── flow.py            # Flow model
│   └── agent_profile.py   # AgentProfile model
├── utils/                 # Utilities
│   ├── terminal.py        # Generate IDs, wait for shell/status
│   ├── logging.py         # File-based logging
│   ├── agent_profiles.py  # Load agent profiles
│   └── template.py        # Template rendering
├── agent_store/           # Agent profile definitions (.md files)
│   ├── developer.md
│   ├── reviewer.md
│   └── code_supervisor.md
└── constants.py           # Application constants
```

## Data Flow Examples

### Terminal Creation Flow
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
inbox_service.register_terminal()  # Starts watchdog observer
  ↓
Returns Terminal model
```

### Inbox Message Flow
```
MCP: send_message(receiver_id, message)
  ↓
API: POST /terminals/{receiver_id}/inbox/messages
  ↓
database.create_inbox_message()  # Status: PENDING
  ↓
inbox_service.check_and_send_pending_messages()
  ↓
If receiver IDLE → send immediately
If receiver BUSY → watchdog monitors log file
  ↓
On log change → detect IDLE pattern → send message
  ↓
Update message status: DELIVERED
```

### Handoff Flow
```
MCP: handoff(agent_profile, message)
  ↓
API: POST /sessions/{session}/terminals
  ↓
Wait for terminal IDLE
  ↓
API: POST /terminals/{id}/input
  ↓
Poll until status = COMPLETED
  ↓
API: GET /terminals/{id}/output?mode=last
  ↓
API: POST /terminals/{id}/exit
  ↓
Return output to caller
```
