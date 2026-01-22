# CAO Enhanced: Beads + Ralph + Web UI

This fork adds three major features to CLI Agent Orchestrator:

## Features

### 1. Beads Integration (Task Queue)

SQLite-backed task queue for work prioritization and persistence.

```bash
# CLI Commands
cao tasks list                    # List all tasks
cao tasks list -s open -p 1       # Filter by status/priority
cao tasks add "Fix bug"           # Add task
cao tasks next                    # Show next priority task
cao tasks wip <id>                # Mark in progress
cao tasks close <id>              # Close task

# Launch with task
cao launch --agents dev --from-queue    # Auto-assign next task
cao launch --agents dev --task abc123   # Specific task
```

### 2. Ralph Loop Integration

Iterative investigation loops with structured feedback.

```bash
cao ralph start "Investigate issue" -n 3 -m 10 -p DONE
cao ralph status                  # Show progress
cao ralph stop                    # Stop loop
```

### 3. Web Dashboard

Browser-based UI at `http://localhost:8000` when running `cao-server`.

Components:
- **Task Panel**: View/add/manage Beads tasks
- **Ralph Panel**: Monitor iterative loops
- **Agent Panel**: View active agents
- **Activity Log**: Real-time updates via WebSocket

## Quick Start

```bash
# Install
pip install -e .

# Build web UI
cd web && npm install && npm run build && cd ..

# Start server (serves API + dashboard)
cao-server

# Open http://localhost:8000
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/tasks` | GET/POST | List/create tasks |
| `/api/tasks/{id}` | GET/PATCH/DELETE | Task CRUD |
| `/api/tasks/{id}/wip` | POST | Mark WIP |
| `/api/tasks/{id}/close` | POST | Close task |
| `/api/tasks/next` | GET | Next priority task |
| `/api/ralph` | GET/POST | Status/start loop |
| `/api/ralph/stop` | POST | Stop loop |
| `/api/ralph/feedback` | POST | Submit feedback |
| `/api/ws/updates` | WS | Real-time updates |

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                         CAO                                  │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │ BeadsClient │  │ RalphRunner │  │   FastAPI + React   │  │
│  │ (SQLite)    │  │ (JSON)      │  │   Web Dashboard     │  │
│  └──────┬──────┘  └──────┬──────┘  └──────────┬──────────┘  │
│         │                │                    │             │
│         └────────────────┴────────────────────┘             │
│                          │                                  │
│                    Orchestrator                             │
└─────────────────────────────────────────────────────────────┘
```
