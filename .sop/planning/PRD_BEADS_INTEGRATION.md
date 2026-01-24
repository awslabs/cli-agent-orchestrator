# PRD: Integrate Real Beads (steveyegge/beads) into CAO

## Overview
Replace the custom SQLite-based `BeadsClient` in CAO with a wrapper around the real Beads library (`beads-mcp` / `bd` CLI) from https://github.com/steveyegge/beads.

## Current State
- CAO has a simple `BeadsClient` in `src/cli_agent_orchestrator/clients/beads.py` using SQLite
- Real Beads is installed: `bd` CLI (v0.48.0) and `beads-mcp` Python package (v0.48.0)
- `bd init` has been run in `~/cao-enhanced` - `.beads/` directory exists
- A wrapper `beads_real.py` was started but has issues with the async BdClient and bd CLI output parsing

## Problem
The `beads-mcp` `BdClient` is async and the bd CLI output format doesn't match expected JSON for `--json` flag on some commands. The wrapper needs to properly handle:
1. Async/sync conversion or use CLI properly
2. Parse bd CLI output correctly (some commands return human-readable text, not JSON)
3. Handle the beads daemon that runs in background

## Requirements

### 1. Create Working BeadsClient Wrapper
File: `src/cli_agent_orchestrator/clients/beads_real.py`

Must implement these methods (matching existing `beads.py` interface):
- `list(status, priority)` → List tasks filtered by status/priority
- `next(priority)` → Get next ready task (no blockers)
- `get(task_id)` → Get single task by ID
- `add(title, description, priority, tags)` → Create new task
- `wip(task_id, assignee)` → Mark task as work-in-progress
- `close(task_id)` → Close a task
- `delete(task_id)` → Delete a task
- `update(task_id, **kwargs)` → Update task fields
- `clear_assignee_by_session(session_id)` → Release tasks assigned to crashed session

### 2. Handle bd CLI Properly
The bd CLI commands and their outputs:
```bash
bd list --json          # May return [] or list of issues
bd ready --json         # Returns ready issues (no blockers)
bd show <id> --json     # Returns single issue
bd create "title" -p N  # Returns "✓ Created issue: <id>" (NOT JSON)
bd update <id> --state in_progress
bd close <id>
bd delete <id> -y
```

Key insight: `bd create` returns human-readable output, need to parse the ID from it.

### 3. Priority Mapping
- CAO uses: 1 (high), 2 (medium), 3 (low)
- Beads uses: P0, P1 (high), P2 (medium), P3, P4 (low)
- Map: CAO 1 → P1, CAO 2 → P2, CAO 3 → P3

### 4. Status Mapping
- CAO uses: "open", "wip", "closed"
- Beads uses: "open", "in_progress", "closed"
- Map: CAO "wip" ↔ Beads "in_progress"

### 5. Update web.py Import
File: `src/cli_agent_orchestrator/api/web.py` line 8

Change from:
```python
from cli_agent_orchestrator.clients.beads import BeadsClient, Task
```
To:
```python
from cli_agent_orchestrator.clients.beads_real import BeadsClient, Task
```

### 6. Dependency Already Added
`pyproject.toml` already has `beads-mcp>=0.48.0` in dependencies.

## Technical Notes

### Option A: Use bd CLI (Recommended)
Shell out to `bd` commands. Simpler but need to parse output correctly.

```python
import subprocess
import json
import re

def _run_bd(self, *args) -> str:
    result = subprocess.run(["bd"] + list(args), capture_output=True, text=True, cwd=self.working_dir)
    return result.stdout.strip()

def add(self, title, priority=2):
    output = self._run_bd("create", title, "-p", str(priority))
    # Parse: "✓ Created issue: abducabd-xyz" → extract "abducabd-xyz"
    match = re.search(r'Created issue:\s*(\S+)', output)
    task_id = match.group(1) if match else ""
    return self.get(task_id)
```

### Option B: Use beads-mcp BdClient (Async)
The `BdClient` methods are async coroutines. Would need to run in event loop:

```python
import asyncio
from beads_mcp.bd_client import create_bd_client

class BeadsClient:
    def __init__(self):
        self._client = create_bd_client()
    
    def list(self):
        return asyncio.run(self._client.list_issues(ListIssuesParams()))
```

### Option C: Use beads-mcp Daemon Client
There's a daemon running (`.beads/bd.sock`). The `bd_daemon_client.py` might provide sync access.

## Test Cases
1. Create task via API → appears in `bd list`
2. List tasks via API → matches `bd list --json`
3. Mark task WIP → `bd show <id>` shows state=in_progress
4. Close task → `bd show <id>` shows state=closed
5. Get ready tasks → matches `bd ready --json`

## Acceptance Criteria
- [ ] `BeadsClient` wrapper works with real Beads
- [ ] All existing API endpoints (`/api/tasks/*`) work unchanged
- [ ] Tasks created via API visible in `bd list`
- [ ] Tasks created via `bd create` visible in API
- [ ] Unit tests pass
- [ ] Dashboard UI works with real Beads backend

## Files to Modify
1. `src/cli_agent_orchestrator/clients/beads_real.py` - Fix/rewrite wrapper
2. `src/cli_agent_orchestrator/api/web.py` - Change import to use beads_real

## Files for Reference
- `src/cli_agent_orchestrator/clients/beads.py` - Original SQLite implementation (interface to match)
- `~/.local/share/mise/installs/python/3.12.7/lib/python3.12/site-packages/beads_mcp/` - beads-mcp source

## Out of Scope
- Beads features beyond basic task CRUD (dependencies, molecules, gates)
- Syncing beads to git (manual `bd sync` for now)
- Multi-repo beads setup
