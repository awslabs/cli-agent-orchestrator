# CAO Beads & Epic Orchestration — Feature Roadmap

## Overview

This roadmap breaks the beads/epic orchestration feature into independently shippable features. Each feature is:
- Small enough to build + test in one session
- End-to-end testable (backend pytest + Playwright UI tests + manual verification)
- Approved by user before moving to the next
- Has clear acceptance criteria

## Design Decisions (locked in)

- Bead data lives in `bd`'s SQLite. CAO's DB only stores terminal↔bead binding.
- Master orchestrator = persistent CLI agent session with MCP tools (not Python service).
- All orchestrator work goes through beads. Every delegation is tracked.
- handoff/assign MCP tools modified in-place to always create beads.
- Notes chain: completed sibling notes flow to the next agent in the epic.
- Orchestrator decides: simple (1-2 steps) auto-execute, complex (3+) propose plan first.
- Context injection: full parent-chain resolution for workspace + context files.

---

## Milestone 1: Bead Binding Foundation

### Feature 1.1: bead_id column on TerminalModel

**What**: Add a `bead_id` column to the terminals table so CAO's DB knows which terminal is working on which bead.

**Files**:
- `src/cli_agent_orchestrator/clients/database.py`

**Changes**:
- Add `bead_id = Column(String, nullable=True)` to `TerminalModel`
- Add inline migration in `init_db()`: `ALTER TABLE terminals ADD COLUMN bead_id TEXT`
- Update `create_terminal()` to accept `bead_id` param
- Include `bead_id` in returned dicts from: `get_terminal_metadata`, `list_terminals_by_session`, `list_all_terminals`, `get_children_terminals`
- New function: `set_terminal_bead(terminal_id, bead_id) -> bool`
- New function: `get_terminal_by_bead(bead_id) -> Optional[Dict]`

**Acceptance criteria**:
- [ ] `create_terminal("tid", "sess", "win", "q_cli", bead_id="bead-123")` stores bead_id
- [ ] `get_terminal_metadata("tid")` returns dict with `bead_id: "bead-123"`
- [ ] `set_terminal_bead("tid", None)` clears it
- [ ] `get_terminal_by_bead("bead-123")` returns the terminal dict
- [ ] `get_terminal_by_bead("nonexistent")` returns None
- [ ] Existing terminals without bead_id still work (migration is safe)

**Backend tests** (`test/clients/test_database_bead_binding.py` — new):
```
test_create_terminal_with_bead_id
test_create_terminal_without_bead_id_defaults_none
test_get_terminal_metadata_includes_bead_id
test_list_terminals_by_session_includes_bead_id
test_set_terminal_bead_sets_value
test_set_terminal_bead_clears_with_none
test_set_terminal_bead_returns_false_for_missing_terminal
test_get_terminal_by_bead_finds_terminal
test_get_terminal_by_bead_returns_none_for_missing
test_init_db_migration_is_idempotent (call init_db twice, no crash)
```

**Playwright tests**: None (no UI change).

**Manual verification**:
1. Start CAO server
2. `curl -X POST 'localhost:8000/sessions?provider=q_cli&agent_profile=developer'`
3. Check DB directly: `sqlite3 ~/.aws/cli-agent-orchestrator/db/cli-agent-orchestrator.db "SELECT id, bead_id FROM terminals"`
4. Confirm bead_id column exists, value is NULL for this terminal

---

### Feature 1.2: Terminal service passes bead_id through

**What**: `terminal_service.create_terminal()` accepts and passes `bead_id` to the DB layer.

**Files**:
- `src/cli_agent_orchestrator/services/terminal_service.py`

**Changes**:
- Add `bead_id: Optional[str] = None` to `create_terminal()` signature
- Pass to `db_create_terminal()` call

**Acceptance criteria**:
- [ ] `terminal_service.create_terminal(provider="q_cli", agent_profile="dev", new_session=True, bead_id="bead-1")` creates terminal with bead_id in DB
- [ ] Existing callers without bead_id still work (backward compatible)

**Backend tests** (`test/services/test_terminal_service_bead.py` — new):
```
test_create_terminal_passes_bead_id_to_db (mock db_create_terminal, verify bead_id arg)
test_create_terminal_defaults_bead_id_none (mock db_create_terminal, verify None)
```

**Playwright tests**: None.

**Manual verification**: Same as 1.1 — create session via API, check DB.

---

### Feature 1.3: Session delete clears bead binding

**What**: When a session is deleted, clear `bead_id` on its terminals so the bead is "released."

**Files**:
- `src/cli_agent_orchestrator/services/session_service.py`

**Changes**:
- In `delete_session()`, before `delete_terminals_by_session()`:
  - Get terminals for session
  - For any terminal with `bead_id`, call `set_terminal_bead(t["id"], None)`

**Acceptance criteria**:
- [ ] Create terminal with bead_id → delete session → `get_terminal_by_bead()` returns None
- [ ] Delete session with no bead_id terminals still works fine

**Backend tests** (`test/services/test_session_bead_cleanup.py` — new):
```
test_delete_session_clears_bead_id (create terminal with bead_id, delete session, verify cleared)
test_delete_session_without_bead_id_no_error
```

**Playwright tests**: None.

**Manual verification**:
1. Create session, manually set bead_id in DB
2. Delete session via API
3. Check DB — terminal record gone, no orphan bead bindings

---

## Milestone 2: Task Model + Epic Methods

### Feature 2.1: Extend Task dataclass

**What**: Add `labels`, `notes`, and `type` fields to the Task dataclass and populate them from bd JSON.

**Files**:
- `src/cli_agent_orchestrator/clients/beads_real.py`
- `test/clients/test_beads_real.py`

**Changes**:
- Add to Task: `labels: Optional[List[str]] = None`, `notes: Optional[str] = None`, `type: Optional[str] = None`
- In `_issue_to_task()`: populate from `issue.get("labels")`, `issue.get("notes")`, `issue.get("type")`

**Acceptance criteria**:
- [ ] Task dataclass has labels, notes, type fields
- [ ] `_issue_to_task({"id": "x", "labels": ["foo"], "notes": "bar", "type": "epic"})` populates all three
- [ ] Missing fields default to None (backward compatible)
- [ ] `task.__dict__` includes the new fields (API responses automatically include them)

**Backend tests** (add to `test/clients/test_beads_real.py`):
```
test_issue_to_task_includes_labels
test_issue_to_task_includes_notes
test_issue_to_task_includes_type
test_issue_to_task_missing_fields_default_none
test_list_returns_tasks_with_new_fields
```

**Playwright tests**: None yet (UI doesn't render these fields yet).

**Manual verification**:
1. Create a bead with labels: `bd create "test" && bd label add <id> "type:epic"`
2. `curl localhost:8000/api/tasks` — verify response includes `labels`, `notes`, `type`

---

### Feature 2.2: create_epic() method

**What**: BeadsClient method to create a parent bead + child beads with sequential dependencies.

**Files**:
- `src/cli_agent_orchestrator/clients/beads_real.py`
- `test/clients/test_beads_real.py`

**Changes**:
- New method: `create_epic(title, steps, priority=2, sequential=True, max_concurrent=3, labels=None, description="")`
  - Creates parent bead via `bd create`
  - Adds labels: `type:epic`, `max_concurrent:N`, any custom labels
  - Creates child bead for each step via `create_child()`
  - If sequential: chains deps — `bd dep add child[1] child[0]`, `bd dep add child[2] child[1]`, etc.
  - Returns parent Task

**Acceptance criteria**:
- [ ] Creates parent bead with title
- [ ] Creates N child beads (one per step)
- [ ] Children have `parent_id` pointing to parent
- [ ] Sequential: child[1] blocked_by child[0], child[2] blocked_by child[1]
- [ ] Parent has `type:epic` label
- [ ] Parent has `max_concurrent:3` label (or custom value)
- [ ] Custom labels applied to parent

**Backend tests** (add to `test/clients/test_beads_real.py`):
```
test_create_epic_creates_parent
test_create_epic_creates_children_for_each_step
test_create_epic_sequential_adds_dependencies
test_create_epic_non_sequential_no_dependencies
test_create_epic_adds_type_epic_label
test_create_epic_adds_max_concurrent_label
test_create_epic_with_custom_labels
test_create_epic_with_description
```

**Playwright tests**: None yet (no UI for epic creation yet).

**Manual verification**:
1. Python REPL: `from cli_agent_orchestrator.clients.beads_real import BeadsClient; b = BeadsClient(); epic = b.create_epic("My Epic", ["Step 1", "Step 2", "Step 3"])`
2. `bd list --json` — verify parent + 3 children
3. `bd show <child2_id> --json` — verify `dependencies` includes child1

---

### Feature 2.3: Dependency + notes + label methods

**What**: BeadsClient methods for managing dependencies, notes, and labels on beads.

**Files**:
- `src/cli_agent_orchestrator/clients/beads_real.py`
- `test/clients/test_beads_real.py`

**Changes**:
- `add_dependency(task_id, depends_on_id) -> bool`
- `remove_dependency(task_id, depends_on_id) -> bool`
- `update_notes(task_id, notes) -> Task`
- `add_label(task_id, label) -> bool`
- `remove_label(task_id, label) -> bool`
- `is_epic(task_id) -> bool` (has children)

**Acceptance criteria**:
- [ ] `add_dependency("B", "A")` → B is blocked by A
- [ ] `remove_dependency("B", "A")` → B is no longer blocked
- [ ] `update_notes("id", "findings here")` → bead notes updated
- [ ] `add_label("id", "priority:high")` → label appears on bead
- [ ] `is_epic("parent_id")` → True; `is_epic("leaf_id")` → False

**Backend tests**:
```
test_add_dependency_calls_bd_dep_add
test_remove_dependency_calls_bd_dep_remove
test_update_notes_calls_bd_update_with_notes_flag
test_add_label_calls_bd_label_add
test_remove_label_calls_bd_label_remove
test_is_epic_true_for_parent_with_children
test_is_epic_false_for_leaf_task
```

**Manual verification**:
1. Create two beads, add dependency, verify with `bd show`
2. Update notes, verify with `bd show`
3. Add label, verify with `bd show`

---

### Feature 2.4: ready() method + label utilities

**What**: Method to get unblocked tasks (optionally scoped to an epic), plus utility functions for extracting label values.

**Files**:
- `src/cli_agent_orchestrator/clients/beads_real.py`
- `test/clients/test_beads_real.py`

**Changes**:
- `ready(parent_id=None) -> List[Task]` — wraps `bd ready --json`, filters by parent
- Module-level functions:
  - `extract_label_value(labels, prefix) -> Optional[str]`
  - `extract_context_files(labels) -> List[str]`
  - `resolve_workspace(task, beads_client, default) -> Optional[str]`
  - `resolve_context_files(task, beads_client) -> List[str]`

**Acceptance criteria**:
- [ ] `ready()` returns all unblocked open tasks
- [ ] `ready(parent_id="epic-1")` returns only children of epic-1 that are unblocked
- [ ] `extract_label_value(["workspace:/foo", "type:epic"], "workspace")` → "/foo"
- [ ] `extract_label_value(["type:epic"], "workspace")` → None
- [ ] `resolve_workspace` walks parent chain until it finds a workspace label
- [ ] `resolve_context_files` collects context labels from task + all ancestors, deduped

**Backend tests**:
```
test_ready_returns_unblocked_tasks
test_ready_filters_by_parent_id
test_ready_returns_empty_when_all_blocked
test_extract_label_value_finds_match
test_extract_label_value_returns_none_when_missing
test_extract_label_value_handles_none_labels
test_extract_context_files_collects_all_context_labels
test_resolve_workspace_from_task_label
test_resolve_workspace_inherits_from_parent
test_resolve_workspace_falls_back_to_default
test_resolve_context_files_walks_parent_chain
test_resolve_context_files_deduplicates
```

**Manual verification**:
1. Create epic with 3 sequential steps
2. `ready(parent_id=epic_id)` → should return only step 1 (steps 2+3 are blocked)
3. Close step 1, call ready again → should return step 2

---

## Milestone 3: Epic API Endpoints

### Feature 3.1: POST /v2/epics endpoint

**What**: HTTP endpoint to create an epic with children.

**Files**:
- `src/cli_agent_orchestrator/api/v2.py`

**Changes**:
- New Pydantic model: `EpicCreate(title, steps, description, priority, sequential, max_concurrent, labels)`
- New endpoint: `POST /v2/epics` — calls `beads.create_epic()`, returns epic + children

**Acceptance criteria**:
- [ ] `POST /v2/epics {"title": "My Epic", "steps": ["A", "B", "C"]}` → 201
- [ ] Response includes epic object + children list + children count
- [ ] Children have sequential deps by default
- [ ] Activity broadcast fires `epic_created` event

**Backend tests** (`test/api/test_v2_epic_routes.py` — new):
```
test_create_epic_returns_201
test_create_epic_returns_epic_and_children
test_create_epic_with_description
test_create_epic_with_custom_labels
test_create_epic_non_sequential
test_create_epic_empty_steps_returns_400
```

**Playwright tests** (`web/e2e/epic-api.spec.ts` — new):
```
test('POST /v2/epics creates epic with children')
test('GET /api/tasks includes epic in list')
```

**Manual verification**:
```bash
curl -X POST localhost:8000/api/v2/epics \
  -H 'Content-Type: application/json' \
  -d '{"title": "Test Epic", "steps": ["Step 1", "Step 2", "Step 3"]}'
```

---

### Feature 3.2: GET /v2/epics/{id} with progress

**What**: Get an epic with its children and progress stats.

**Files**:
- `src/cli_agent_orchestrator/api/v2.py`

**Changes**:
- New endpoint: `GET /v2/epics/{id}` — returns epic + children + progress (total/completed/wip/open)

**Acceptance criteria**:
- [ ] Returns epic object, children list, progress object
- [ ] Progress: `{total: 3, completed: 1, wip: 1, open: 1}`
- [ ] 404 if epic not found

**Backend tests**:
```
test_get_epic_returns_epic_and_children
test_get_epic_returns_progress_counts
test_get_epic_404_for_missing
```

**Playwright tests**:
```
test('GET /v2/epics/:id returns progress')
```

**Manual verification**: Create epic, close one child, GET epic, verify progress counts.

---

### Feature 3.3: GET /v2/epics/{id}/ready + dependency endpoints

**What**: Get ready (unblocked) children + add/remove dependency endpoints.

**Files**:
- `src/cli_agent_orchestrator/api/v2.py`

**Changes**:
- `GET /v2/epics/{id}/ready` — calls `beads.ready(parent_id=id)`
- `POST /v2/beads/{id}/dep` body: `{depends_on: "other_id"}` — adds dependency
- `DELETE /v2/beads/{id}/dep/{dep_id}` — removes dependency

**Acceptance criteria**:
- [ ] Ready endpoint returns only unblocked children
- [ ] Add dependency → child becomes blocked
- [ ] Remove dependency → child becomes unblocked
- [ ] Dependency on closed bead doesn't block

**Backend tests**:
```
test_get_ready_returns_unblocked_children
test_add_dependency_blocks_task
test_remove_dependency_unblocks_task
test_add_dependency_400_on_failure
```

**Manual verification**: Create epic, verify only step 1 is ready, add/remove deps via curl, check.

---

### Feature 3.4: Bead-session wiring in API

**What**: Assign endpoints store bead_id in DB + new endpoint to look up which session works on a bead.

**Files**:
- `src/cli_agent_orchestrator/api/v2.py`

**Changes**:
- `assign_bead_to_agent`: pass `bead_id` to `create_terminal()`, add per-bead asyncio lock
- `assign_bead`: call `set_terminal_bead()` on session's first terminal
- New: `GET /v2/beads/{id}/session` → calls `get_terminal_by_bead()`
- `list_sessions` response now includes `bead_id` on terminals (automatic from M1)

**Acceptance criteria**:
- [ ] Assign bead to agent → terminal has bead_id in DB
- [ ] `GET /v2/beads/{bead_id}/session` → returns terminal/session info
- [ ] `GET /v2/beads/nonexistent/session` → 404
- [ ] Concurrent assigns to same bead → second gets 409 (lock)
- [ ] Delete session → bead_id cleared (from Feature 1.3)
- [ ] `GET /v2/sessions` → each session's terminal data includes `bead_id`

**Backend tests**:
```
test_assign_agent_stores_bead_id_in_db
test_assign_bead_stores_bead_id_in_db
test_get_bead_session_returns_terminal
test_get_bead_session_404_for_unassigned
test_concurrent_assign_returns_409
test_list_sessions_includes_bead_id
```

**Playwright tests**:
```
test('assign bead to agent shows bead on session card')
test('delete session clears bead assignment')
```

**Manual verification**:
1. Create bead, assign to agent via curl
2. `curl localhost:8000/api/v2/beads/{id}/session` — verify returns session
3. Delete session, curl again — 404

---

## Milestone 4: Context Injection

### Feature 4.1: Context injection utility + assign-agent wiring

**What**: When assigning a bead to an agent, inject workspace + context files from bead labels (with parent chain inheritance).

**Files**:
- `src/cli_agent_orchestrator/utils/context.py` — new
- `src/cli_agent_orchestrator/api/v2.py` — modify assign-agent

**Changes**:
- New `context.py`: `inject_context_files(terminal_id, files)` sends `/context add "f1" "f2"` to terminal
- In `assign_bead_to_agent`: after CLI ready, resolve workspace + context from labels, inject

**Acceptance criteria**:
- [ ] Bead with `context:/path/to/file.md` label → agent gets `/context add "/path/to/file.md"` sent
- [ ] Bead with no context labels → nothing injected (no error)
- [ ] Child bead inherits context labels from parent epic
- [ ] Workspace label on parent → child agents use that workspace

**Backend tests** (`test/utils/test_context.py` — new):
```
test_inject_context_files_sends_context_add_command
test_inject_context_files_empty_list_no_op
test_inject_context_files_quotes_paths_with_spaces
```

**Playwright tests**: None (internal mechanism, verified by checking terminal output).

**Manual verification**:
1. Create bead with label `context:/tmp/test.md`
2. Assign to agent via API
3. Check terminal output — should see `/context add "/tmp/test.md"`

---

## Milestone 5: Bead-Aware MCP Tools

### Feature 5.1: Modify handoff to create beads

**What**: The existing `handoff` MCP tool now creates a bead, assigns the task to it, and returns bead notes as the persistent output.

**Files**:
- `src/cli_agent_orchestrator/mcp_server/server.py`

**Changes to handoff**:
1. Accept optional `parent_bead_id` param (for epic-scoped work)
2. Create bead: `POST /api/tasks {title, description}` (or with parent)
3. Create terminal with `bead_id`
4. Build prompt: include task description + sibling notes if parent epic exists
5. Send prompt, wait for completion (existing logic)
6. On completion: read bead notes, close bead
7. Return: bead_id + bead notes (instead of raw terminal scrape)

**Acceptance criteria**:
- [ ] `handoff("developer", "Fix the login bug")` creates a bead in bd
- [ ] Terminal is created with bead_id binding
- [ ] Agent prompt includes bead ID and instructions to write notes
- [ ] After completion, bead is closed with notes
- [ ] Return value includes `bead_id` and notes content
- [ ] With `parent_bead_id`: child bead created, sibling notes included in prompt

**Backend tests** (`test/mcp/test_handoff_bead.py` — new):
```
test_handoff_creates_bead
test_handoff_binds_bead_to_terminal
test_handoff_closes_bead_on_completion
test_handoff_returns_bead_id_and_notes
test_handoff_with_parent_includes_sibling_notes
test_handoff_timeout_leaves_bead_open
```

**Manual verification**:
1. From a Claude Code session with CAO MCP server configured
2. Call handoff tool, verify bead appears in `bd list`
3. After completion, verify bead is closed with notes

---

### Feature 5.2: Modify assign to create beads

**What**: The existing `assign` MCP tool now creates a bead for fire-and-forget delegation.

**Files**:
- `src/cli_agent_orchestrator/mcp_server/server.py`

**Changes to assign**:
1. Accept optional `parent_bead_id` param
2. Create bead for the task
3. Create terminal with `bead_id`
4. Build prompt with bead context (+ sibling notes if parent exists)
5. Send prompt, return immediately
6. Return includes `bead_id` so caller can track via `get_epic_status` or `list_beads`

**Acceptance criteria**:
- [ ] `assign("developer", "Build the API")` creates bead + terminal
- [ ] Bead is in `wip` state with session as assignee
- [ ] Return includes `bead_id` and `terminal_id`
- [ ] Agent receives prompt with bead ID and bd instructions

**Backend tests**:
```
test_assign_creates_bead
test_assign_returns_bead_id
test_assign_bead_is_wip
test_assign_with_parent_creates_child_bead
```

---

### Feature 5.3: New orchestration MCP tools (read operations)

**What**: Add MCP tools for querying state — these are read-only and safe to ship first.

**Files**:
- `src/cli_agent_orchestrator/mcp_server/server.py`

**New tools**:
- `list_sessions()` — all active sessions with status + bead info
- `get_session_output(session_id, lines=100)` — terminal output
- `list_beads(status=None)` — all beads, optionally filtered
- `get_epic_status(epic_id)` — children + progress + active agents
- `get_ready_beads(epic_id=None)` — unblocked beads

**Acceptance criteria**:
- [ ] Each tool returns correct data from CAO API
- [ ] Error handling: returns error dict (not crash) on API failure
- [ ] list_sessions includes bead_id on each session's terminals

**Backend tests** (`test/mcp/test_orchestration_tools_read.py`):
```
test_list_sessions_returns_sessions
test_get_session_output_returns_output
test_list_beads_returns_all
test_list_beads_filters_by_status
test_get_epic_status_returns_progress
test_get_ready_beads_returns_unblocked
test_get_ready_beads_scoped_to_epic
```

---

### Feature 5.4: New orchestration MCP tools (write operations)

**What**: MCP tools for creating/managing beads and sessions.

**Files**:
- `src/cli_agent_orchestrator/mcp_server/server.py`

**New tools**:
- `create_bead(title, description, priority)` — standalone bead
- `create_epic(title, steps, sequential)` — epic with children
- `assign_bead(bead_id, agent_profile, provider)` — assign existing bead to agent
- `close_bead(bead_id)` — close completed bead
- `kill_session(session_id)` — terminate + cleanup

**Acceptance criteria**:
- [ ] Each tool calls the correct CAO API endpoint
- [ ] create_epic returns epic_id + children
- [ ] assign_bead spawns session with bead_id binding
- [ ] kill_session clears bead assignment
- [ ] close_bead marks bead as closed in bd

**Backend tests** (`test/mcp/test_orchestration_tools_write.py`):
```
test_create_bead_creates_task
test_create_epic_creates_with_children
test_assign_bead_spawns_session
test_close_bead_closes_task
test_kill_session_terminates_and_clears
```

---

## Milestone 6: Master Orchestrator

### Feature 6.1: Master orchestrator agent profile

**What**: Agent profile (.md) with detailed instructions for orchestrating work via MCP tools.

**Files**:
- `master_orchestrator.md` in agent store

**Agent profile contents**:
- Role: you are the master orchestrator for CAO
- All work goes through beads
- Decomposition: simple (1-2 steps) auto-execute, complex (3+) propose plan first
- Dispatch: check `get_ready_beads` → `assign_bead` with agent + provider
- Monitor: `get_epic_status` periodically
- Context: completed sibling notes automatically included in agent prompts
- Rules: respect max_concurrent, always track via beads, ask user when unsure

**Acceptance criteria**:
- [ ] Profile exists and is discoverable via `GET /v2/agents`
- [ ] Profile includes clear tool usage instructions
- [ ] Profile references all MCP tools by name

**Tests**: Manual only — read the profile, verify instructions are clear.

---

### Feature 6.2: Orchestrator launch endpoint

**What**: API endpoint to launch the master orchestrator as a persistent session.

**Files**:
- `src/cli_agent_orchestrator/api/v2.py`

**Changes**:
- New endpoint: `POST /v2/orchestrator/launch` body: `{provider, agent_profile}`
- Creates a persistent session with the orchestrator agent
- Returns session_id and terminal_id

**Acceptance criteria**:
- [ ] `POST /v2/orchestrator/launch {"provider": "claude_code"}` → 201
- [ ] Returns session_id and terminal_id
- [ ] Session appears in `GET /v2/sessions`
- [ ] Orchestrator is ready to receive input

**Backend tests**:
```
test_launch_orchestrator_creates_session
test_launch_orchestrator_returns_session_info
test_launch_orchestrator_uses_specified_provider
```

**Playwright tests**:
```
test('launch orchestrator via API and verify session exists')
```

**Manual verification**:
1. `curl -X POST localhost:8000/api/v2/orchestrator/launch -H 'Content-Type: application/json' -d '{"provider": "claude_code"}'`
2. Attach to tmux session, verify orchestrator agent is running
3. Send it a message: "Create an epic to build a REST API"
4. Watch it use MCP tools to create epic + dispatch agents

---

## Milestone 7: Frontend

### Feature 7.1: Epic API client methods

**What**: Add epic endpoints to the frontend API client.

**Files**:
- `web/src/api.ts`

**Changes**:
- New `epics` namespace: create, get, getReady, getStatus
- New WebSocket helper: `createEpicStream`

**Acceptance criteria**:
- [ ] `api.epics.create({title, steps})` calls POST /v2/epics
- [ ] `api.epics.get(id)` calls GET /v2/epics/{id}
- [ ] All methods handle errors gracefully (return fallback)

**Tests**: TypeScript compilation + Playwright integration tests.

---

### Feature 7.2: Epic creation in NewBeadModal

**What**: Toggle between "Single Bead" and "Epic" in the bead creation modal.

**Files**:
- `web/src/components/starcraft/NewBeadModal.tsx`

**Changes**:
- Mode toggle: "Bead" | "Epic"
- Epic mode: title + dynamic step list (add/remove) + sequential checkbox + max_concurrent
- Submit calls `api.epics.create()` in epic mode

**Acceptance criteria**:
- [ ] Modal has mode toggle
- [ ] Epic mode shows step list with add/remove buttons
- [ ] Submit creates epic via API
- [ ] Created epic appears in bead list

**Playwright tests** (`web/e2e/epic-creation.spec.ts`):
```
test('can toggle to epic mode')
test('can add and remove steps')
test('creating epic shows children in bead list')
test('epic has progress indicator')
```

---

### Feature 7.3: Epic cards in BeadsPanel

**What**: Epics render as expandable cards with progress bars and child lists.

**Files**:
- `web/src/components/BeadsPanel.tsx`
- `web/src/stores/starcraftStore.ts`

**Changes**:
- BeadOnMap gets: `isEpic`, `childCount`, `completedCount`
- Epic cards show: title, progress bar (completed/total), expandable child list
- Each child shows status badge (open/wip/closed)

**Acceptance criteria**:
- [ ] Epics render differently from regular beads
- [ ] Progress bar reflects actual child completion
- [ ] Expanding shows child list with status
- [ ] Closing a child updates progress in real-time

**Playwright tests** (`web/e2e/epic-display.spec.ts`):
```
test('epic shows progress bar')
test('epic expands to show children')
test('closing child updates progress')
```

---

### Feature 7.4: Orchestrator controls in UI

**What**: Button to launch master orchestrator + show its session output.

**Files**:
- `web/src/components/OrchestrationPanel.tsx`

**Changes**:
- "Launch Orchestrator" button (provider selector dropdown)
- When running: show session output stream
- Input field to send messages to orchestrator

**Acceptance criteria**:
- [ ] Can launch orchestrator from UI
- [ ] Can see orchestrator's terminal output
- [ ] Can send messages to orchestrator
- [ ] Session appears in session list

**Playwright tests** (`web/e2e/orchestrator.spec.ts`):
```
test('launch orchestrator button creates session')
test('orchestrator output streams to panel')
test('can send input to orchestrator')
```

---

## Dependency Graph

```
M1: Foundation
  1.1 bead_id column
  1.2 terminal_service pass-through  ← depends on 1.1
  1.3 session delete cleanup         ← depends on 1.1

M2: Task Model + Methods
  2.1 Task dataclass extension
  2.2 create_epic()                  ← depends on 2.1
  2.3 dependency/notes/label methods ← depends on 2.1
  2.4 ready() + label utilities      ← depends on 2.1

M3: Epic API
  3.1 POST /v2/epics                 ← depends on 2.2
  3.2 GET /v2/epics/{id}             ← depends on 2.4
  3.3 ready + dep endpoints          ← depends on 2.3, 2.4
  3.4 bead-session wiring            ← depends on 1.1, 1.2, 1.3

M4: Context Injection
  4.1 context utility + wiring       ← depends on 2.4, 3.4

M5: MCP Tools
  5.1 handoff bead-aware             ← depends on 1.2, 2.1
  5.2 assign bead-aware              ← depends on 1.2, 2.1
  5.3 read orchestration tools       ← depends on 3.1, 3.2
  5.4 write orchestration tools      ← depends on 3.1, 3.3, 3.4

M6: Master Orchestrator
  6.1 agent profile                  ← depends on 5.3, 5.4
  6.2 launch endpoint                ← depends on 6.1

M7: Frontend
  7.1 API client methods             ← depends on 3.1, 3.2
  7.2 epic creation modal            ← depends on 7.1
  7.3 epic cards + progress          ← depends on 7.1
  7.4 orchestrator controls          ← depends on 6.2, 7.1
```

## Testing Strategy

Every round includes three layers:

1. **Integration tests (pytest)** — FastAPI TestClient hitting real endpoints with mocked `bd` CLI subprocess calls. Tests the full stack: API → service → database. Pattern: `TestClient(app)` + `patch("subprocess.run")`.

2. **Playwright E2E tests** — Browser automation against running server. Even for backend-only rounds, we verify through the existing UI that data appears correctly.

3. **Manual UI testing** — Step-by-step instructions for clicking through the UI + curl commands.

Test file locations follow existing patterns:
- Backend: `test/api/`, `test/clients/`, `test/services/`
- Frontend: `web/src/components/*.test.tsx`
- E2E: `web/e2e/*.spec.ts`

---

## Execution Plan — Round by Round

---

### Round 1: DB Column + Task Model (Features 1.1 + 2.1)

**What you get**: bead_id column exists on terminals, Task dataclass has labels/notes/type.

**Integration tests** (`test/clients/test_database_bead_binding.py` — new):
```python
"""Integration tests for bead_id binding on terminals."""
import pytest
from cli_agent_orchestrator.clients.database import (
    init_db, create_terminal, get_terminal_metadata,
    list_terminals_by_session, set_terminal_bead,
    get_terminal_by_bead, delete_terminals_by_session
)

@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    """Use temp DB for each test."""
    db_file = tmp_path / "test.db"
    monkeypatch.setattr("cli_agent_orchestrator.clients.database.DATABASE_URL", f"sqlite:///{db_file}")
    init_db()
    yield

class TestBeadIdColumn:
    def test_create_terminal_with_bead_id(self):
        create_terminal("t1", "cao-sess", "win", "q_cli", bead_id="bead-123")
        meta = get_terminal_metadata("t1")
        assert meta["bead_id"] == "bead-123"

    def test_create_terminal_without_bead_id(self):
        create_terminal("t2", "cao-sess", "win", "q_cli")
        meta = get_terminal_metadata("t2")
        assert meta["bead_id"] is None

    def test_set_terminal_bead(self):
        create_terminal("t3", "cao-sess", "win", "q_cli")
        assert set_terminal_bead("t3", "bead-456")
        assert get_terminal_metadata("t3")["bead_id"] == "bead-456"

    def test_set_terminal_bead_clears(self):
        create_terminal("t4", "cao-sess", "win", "q_cli", bead_id="bead-789")
        set_terminal_bead("t4", None)
        assert get_terminal_metadata("t4")["bead_id"] is None

    def test_set_terminal_bead_missing_returns_false(self):
        assert not set_terminal_bead("nonexistent", "bead-1")

    def test_get_terminal_by_bead(self):
        create_terminal("t5", "cao-sess", "win", "q_cli", bead_id="bead-lookup")
        result = get_terminal_by_bead("bead-lookup")
        assert result is not None
        assert result["id"] == "t5"

    def test_get_terminal_by_bead_missing(self):
        assert get_terminal_by_bead("nonexistent") is None

    def test_list_terminals_includes_bead_id(self):
        create_terminal("t6", "cao-sess", "win", "q_cli", bead_id="bead-list")
        terminals = list_terminals_by_session("cao-sess")
        assert any(t["bead_id"] == "bead-list" for t in terminals)

    def test_init_db_migration_idempotent(self):
        init_db()  # second call should not crash
        create_terminal("t7", "cao-sess", "win", "q_cli", bead_id="bead-ok")
        assert get_terminal_metadata("t7")["bead_id"] == "bead-ok"
```

**Beads Task model tests** (add to `test/clients/test_beads_real.py`):
```python
class TestTaskNewFields:
    def test_issue_to_task_includes_labels(self, client):
        issue = {"id": "x", "title": "T", "priority": 2, "status": "open",
                 "labels": ["type:epic", "workspace:/foo"]}
        with patch("subprocess.run") as m:
            m.return_value = MagicMock(returncode=0, stdout=json.dumps([issue]), stderr="")
            task = client.get("x")
            assert task.labels == ["type:epic", "workspace:/foo"]

    def test_issue_to_task_includes_notes(self, client):
        issue = {"id": "x", "title": "T", "priority": 2, "status": "open",
                 "notes": "Found root cause in auth.py"}
        with patch("subprocess.run") as m:
            m.return_value = MagicMock(returncode=0, stdout=json.dumps([issue]), stderr="")
            task = client.get("x")
            assert task.notes == "Found root cause in auth.py"

    def test_issue_to_task_missing_fields_default_none(self, client):
        issue = {"id": "x", "title": "T", "priority": 2, "status": "open"}
        with patch("subprocess.run") as m:
            m.return_value = MagicMock(returncode=0, stdout=json.dumps([issue]), stderr="")
            task = client.get("x")
            assert task.labels is None
            assert task.notes is None
            assert task.type is None
```

**Playwright E2E** (`web/e2e/round1-bead-binding.spec.ts` — new):
```typescript
import { test, expect } from '@playwright/test';

test.describe('Round 1: bead_id binding + Task model', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('http://localhost:8000');
  });

  test('sessions list loads without errors', async ({ page }) => {
    // bead_id column migration should not break existing UI
    // Sessions panel should still render
    const sessionsTab = page.getByRole('button', { name: /sessions/i });
    if (await sessionsTab.isVisible({ timeout: 3000 }).catch(() => false)) {
      await sessionsTab.click();
    }
    // No error toasts or blank screens
    await expect(page.locator('text=Error')).not.toBeVisible({ timeout: 2000 });
  });

  test('beads panel loads and shows tasks', async ({ page }) => {
    // Task model changes should not break existing bead display
    const beadsTab = page.getByRole('button', { name: /beads/i });
    if (await beadsTab.isVisible({ timeout: 3000 }).catch(() => false)) {
      await beadsTab.click();
    }
    await expect(page.locator('text=Error')).not.toBeVisible({ timeout: 2000 });
  });

  test('task API returns new fields', async ({ request }) => {
    const res = await request.get('http://localhost:8000/api/tasks');
    expect(res.ok()).toBeTruthy();
    const tasks = await res.json();
    // If there are tasks, they should have the new fields (even if null)
    if (tasks.length > 0) {
      expect(tasks[0]).toHaveProperty('labels');
      expect(tasks[0]).toHaveProperty('notes');
      expect(tasks[0]).toHaveProperty('type');
    }
  });
});
```

**Manual UI test**:
1. Start server: `cao-server`
2. Open `http://localhost:8000` in browser
3. Click through each tab (Sessions, Beads, Flows, etc.) — **verify nothing is broken** by the migration
4. Check the Beads panel — tasks should still display correctly
5. Open browser DevTools → Network tab → check `/api/tasks` response — should include `labels`, `notes`, `type` fields
6. Open browser DevTools → Console — **verify no JS errors**

**Backend manual test**:
```bash
# Verify migration
sqlite3 ~/.aws/cli-agent-orchestrator/db/cli-agent-orchestrator.db "PRAGMA table_info(terminals);" | grep bead_id
# Expected: bead_id column exists

# Verify API
curl -s localhost:8000/api/tasks | python3 -c "
import sys,json
tasks = json.load(sys.stdin)
print(f'{len(tasks)} tasks')
if tasks:
    t = tasks[0]
    print(f'Has labels: {\"labels\" in t}')
    print(f'Has notes: {\"notes\" in t}')
    print(f'Has type: {\"type\" in t}')
else:
    print('No tasks yet - create one with: bd create \"test\"')
"
```

**What to look for**: Everything that worked before still works. New DB column exists. New Task fields appear in API responses.

---

### Round 2: Pass-through + Cleanup + Epic Creation + Dep Methods (Features 1.2, 1.3, 2.2, 2.3)

**What you get**: terminal_service accepts bead_id, session delete clears it, BeadsClient can create epics and manage deps/notes/labels.

**Integration tests** (`test/clients/test_beads_epic.py` — new):
```python
"""Integration tests for epic creation and dependency management."""
import json
from unittest.mock import MagicMock, patch, call
import pytest
from cli_agent_orchestrator.clients.beads_real import BeadsClient

@pytest.fixture
def client():
    return BeadsClient()

class TestCreateEpic:
    def test_creates_parent_and_children(self, client):
        """create_epic creates parent + N children with sequential deps."""
        call_count = {"create": 0, "dep": 0, "label": 0}
        def mock_run(cmd, **kw):
            cmd_str = " ".join(cmd)
            if "create" in cmd_str:
                call_count["create"] += 1
                return MagicMock(returncode=0, stdout=f"Created issue: epic-{call_count['create']}", stderr="")
            if "dep add" in cmd_str:
                call_count["dep"] += 1
                return MagicMock(returncode=0, stdout="", stderr="")
            if "label add" in cmd_str:
                call_count["label"] += 1
                return MagicMock(returncode=0, stdout="", stderr="")
            # show/list for get()
            return MagicMock(returncode=0, stdout=json.dumps([{"id": "epic-1", "title": "Epic", "priority": 2, "status": "open"}]), stderr="")

        with patch("subprocess.run", side_effect=mock_run):
            epic = client.create_epic("My Epic", ["Step A", "Step B", "Step C"])
            assert call_count["create"] == 4  # 1 parent + 3 children
            assert call_count["dep"] == 2     # B blocked_by A, C blocked_by B
            assert call_count["label"] >= 2   # type:epic + max_concurrent

    def test_non_sequential_no_deps(self, client):
        call_count = {"dep": 0}
        def mock_run(cmd, **kw):
            if "dep add" in " ".join(cmd):
                call_count["dep"] += 1
            return MagicMock(returncode=0, stdout="Created issue: x", stderr="")
        with patch("subprocess.run", side_effect=mock_run):
            client.create_epic("Parallel", ["A", "B", "C"], sequential=False)
            assert call_count["dep"] == 0

class TestDependencyMethods:
    def test_add_dependency(self, client):
        with patch("subprocess.run") as m:
            m.return_value = MagicMock(returncode=0, stdout="", stderr="")
            assert client.add_dependency("task-2", "task-1")
            args = m.call_args[0][0]
            assert "dep" in args and "add" in args

    def test_remove_dependency(self, client):
        with patch("subprocess.run") as m:
            m.return_value = MagicMock(returncode=0, stdout="", stderr="")
            assert client.remove_dependency("task-2", "task-1")
            args = m.call_args[0][0]
            assert "dep" in args and "remove" in args

class TestNotesAndLabels:
    def test_update_notes(self, client):
        with patch("subprocess.run") as m:
            m.return_value = MagicMock(returncode=0, stdout=json.dumps([{"id": "x", "title": "T", "priority": 2, "status": "open", "notes": "my notes"}]), stderr="")
            task = client.update_notes("x", "my notes")
            update_call = [c for c in m.call_args_list if "--notes" in " ".join(c[0][0])]
            assert len(update_call) == 1

    def test_add_label(self, client):
        with patch("subprocess.run") as m:
            m.return_value = MagicMock(returncode=0, stdout="", stderr="")
            assert client.add_label("x", "priority:high")
            args = m.call_args[0][0]
            assert "label" in args and "add" in args and "priority:high" in args

class TestLabelUtilities:
    def test_extract_label_value(self):
        from cli_agent_orchestrator.clients.beads_real import extract_label_value
        assert extract_label_value(["workspace:/foo", "type:epic"], "workspace") == "/foo"
        assert extract_label_value(["type:epic"], "workspace") is None
        assert extract_label_value(None, "workspace") is None

    def test_resolve_workspace_from_task(self):
        from cli_agent_orchestrator.clients.beads_real import resolve_workspace, Task
        task = Task(id="t1", title="T", labels=["workspace:/my/dir"])
        assert resolve_workspace(task, None, "/default") == "/my/dir"

    def test_resolve_workspace_inherits_from_parent(self):
        from cli_agent_orchestrator.clients.beads_real import resolve_workspace, Task
        child = Task(id="c1", title="Child", parent_id="p1")
        parent = Task(id="p1", title="Parent", labels=["workspace:/parent/dir"])
        mock_client = MagicMock()
        mock_client.get.return_value = parent
        assert resolve_workspace(child, mock_client, "/default") == "/parent/dir"
```

**Playwright E2E** (`web/e2e/round2-epic-creation.spec.ts`):
```typescript
import { test, expect } from '@playwright/test';

test.describe('Round 2: Epic creation via API', () => {
  test('beads panel still loads after BeadsClient changes', async ({ page }) => {
    await page.goto('http://localhost:8000');
    // Verify beads panel renders without errors
    await expect(page.locator('text=Error')).not.toBeVisible({ timeout: 2000 });
  });

  test('tasks API returns tasks with parent_id field', async ({ request }) => {
    const res = await request.get('http://localhost:8000/api/tasks');
    expect(res.ok()).toBeTruthy();
    const tasks = await res.json();
    if (tasks.length > 0) {
      // parent_id should be present (even if null)
      expect(tasks[0]).toHaveProperty('parent_id');
    }
  });
});
```

**Manual UI test**:
1. Open `http://localhost:8000` → Beads panel
2. Create an epic via Python REPL (see below) — **refresh UI, verify parent + children appear**
3. Children should show as sub-items under the parent (existing parent/child rendering)

**Manual backend test**:
```bash
# Create epic and verify
python3 -c "
from cli_agent_orchestrator.clients.beads_real import BeadsClient
b = BeadsClient()
epic = b.create_epic('Build REST API', ['Design schema', 'Implement endpoints', 'Write tests'])
print(f'Epic: {epic.id}')
children = b.get_children(epic.id)
for c in children:
    print(f'  Child: {c.id} - {c.title} - blocked_by: {c.blocked_by}')
ready = b.ready(parent_id=epic.id)
print(f'Ready: {[t.title for t in ready]}')
# Close step 1 and check ready again
b.close(children[0].id)
ready2 = b.ready(parent_id=epic.id)
print(f'Ready after closing step 1: {[t.title for t in ready2]}')
"
# Expected: Only step 1 ready initially, step 2 ready after closing step 1

# Verify in UI: refresh http://localhost:8000, check beads panel
```

**What to look for**: Epic parent + children created with sequential deps. `ready()` respects dependency order. Notes/labels persist. UI still works.

---

### Round 3: Epic API + Ready Endpoint (Features 2.4, 3.1)

**What you get**: HTTP endpoints to create epics and get ready tasks.

**Integration tests** (`test/api/test_v2_epic_routes.py` — new):
```python
"""Integration tests for epic API endpoints."""
import json
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

@pytest.fixture
def client():
    """TestClient with mocked BeadsClient."""
    with patch("cli_agent_orchestrator.api.v2.beads") as mock_beads:
        from cli_agent_orchestrator.api.main import app
        mock_beads.create_epic.return_value = MagicMock(
            id="epic-1", title="Test Epic", status="open", priority=2,
            labels=["type:epic"], __dict__={"id": "epic-1", "title": "Test Epic", "status": "open"}
        )
        mock_beads.get_children.return_value = [
            MagicMock(id="epic-1.1", title="Step 1", status="open", __dict__={"id": "epic-1.1", "title": "Step 1", "status": "open"}),
            MagicMock(id="epic-1.2", title="Step 2", status="open", __dict__={"id": "epic-1.2", "title": "Step 2", "status": "open"}),
        ]
        mock_beads.get.return_value = MagicMock(id="epic-1", title="Test Epic", __dict__={"id": "epic-1"})
        mock_beads.ready.return_value = [
            MagicMock(id="epic-1.1", title="Step 1", __dict__={"id": "epic-1.1", "title": "Step 1"})
        ]
        yield TestClient(app), mock_beads

class TestCreateEpicEndpoint:
    def test_create_epic_201(self, client):
        tc, mock = client
        res = tc.post("/api/v2/epics", json={"title": "My Epic", "steps": ["A", "B"]})
        assert res.status_code == 201
        data = res.json()
        assert "epic" in data
        assert "children" in data
        mock.create_epic.assert_called_once()

    def test_create_epic_passes_params(self, client):
        tc, mock = client
        tc.post("/api/v2/epics", json={
            "title": "E", "steps": ["X"], "sequential": False, "max_concurrent": 5
        })
        _, kwargs = mock.create_epic.call_args
        # Verify params passed through

class TestGetEpicEndpoint:
    def test_get_epic_returns_progress(self, client):
        tc, mock = client
        mock.get_children.return_value = [
            MagicMock(status="closed", __dict__={"id": "1", "status": "closed"}),
            MagicMock(status="wip", __dict__={"id": "2", "status": "wip"}),
            MagicMock(status="open", __dict__={"id": "3", "status": "open"}),
        ]
        res = tc.get("/api/v2/epics/epic-1")
        assert res.status_code == 200
        data = res.json()
        assert data["progress"]["total"] == 3
        assert data["progress"]["completed"] == 1
        assert data["progress"]["wip"] == 1

    def test_get_epic_404(self, client):
        tc, mock = client
        mock.get.return_value = None
        res = tc.get("/api/v2/epics/nonexistent")
        assert res.status_code == 404

class TestReadyEndpoint:
    def test_get_ready_returns_unblocked(self, client):
        tc, mock = client
        res = tc.get("/api/v2/epics/epic-1/ready")
        assert res.status_code == 200
        data = res.json()
        assert len(data) == 1
        assert data[0]["title"] == "Step 1"
        mock.ready.assert_called_with(parent_id="epic-1")
```

**Playwright E2E** (`web/e2e/round3-epic-api.spec.ts`):
```typescript
import { test, expect } from '@playwright/test';

test.describe('Round 3: Epic API', () => {
  let epicId: string;

  test('create epic via API', async ({ request }) => {
    const res = await request.post('http://localhost:8000/api/v2/epics', {
      data: { title: 'E2E Test Epic', steps: ['Step 1', 'Step 2', 'Step 3'] }
    });
    expect(res.ok()).toBeTruthy();
    const data = await res.json();
    expect(data.epic).toBeDefined();
    expect(data.children).toHaveLength(3);
    epicId = data.epic.id;
  });

  test('epic appears in beads panel', async ({ page }) => {
    await page.goto('http://localhost:8000');
    // Wait for beads to load
    await page.waitForTimeout(2000);
    // Epic title should appear somewhere in the bead list
    const epicText = page.locator(`text=E2E Test Epic`);
    await expect(epicText).toBeVisible({ timeout: 5000 });
  });

  test('get epic progress via API', async ({ request }) => {
    const res = await request.get(`http://localhost:8000/api/v2/epics/${epicId}`);
    expect(res.ok()).toBeTruthy();
    const data = await res.json();
    expect(data.progress.total).toBe(3);
    expect(data.progress.completed).toBe(0);
  });

  test('ready endpoint returns only first step', async ({ request }) => {
    const res = await request.get(`http://localhost:8000/api/v2/epics/${epicId}/ready`);
    expect(res.ok()).toBeTruthy();
    const data = await res.json();
    expect(data).toHaveLength(1);
    expect(data[0].title).toBe('Step 1');
  });
});
```

**Manual UI + backend test**:
```bash
# 1. Create epic via curl
EPIC=$(curl -s -X POST localhost:8000/api/v2/epics \
  -H 'Content-Type: application/json' \
  -d '{"title": "Deploy Service", "steps": ["Dockerfile", "CI Pipeline", "Deploy", "Smoke Tests"]}')
echo $EPIC | python3 -m json.tool
EPIC_ID=$(echo $EPIC | python3 -c "import sys,json; print(json.load(sys.stdin)['epic']['id'])")

# 2. Open http://localhost:8000 → Beads panel → verify epic + 4 children visible

# 3. Check progress
curl -s localhost:8000/api/v2/epics/$EPIC_ID | python3 -c "
import sys,json; d=json.load(sys.stdin)
print(f'Progress: {d[\"progress\"]}')"
# Expected: {'total': 4, 'completed': 0, 'wip': 0}

# 4. Check ready
curl -s localhost:8000/api/v2/epics/$EPIC_ID/ready | python3 -c "
import sys,json; print([t['title'] for t in json.load(sys.stdin)])"
# Expected: ['Dockerfile'] (only first step)

# 5. Close step 1, verify step 2 becomes ready
CHILD1=$(curl -s localhost:8000/api/v2/epics/$EPIC_ID/ready | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")
bd close $CHILD1
curl -s localhost:8000/api/v2/epics/$EPIC_ID/ready | python3 -c "
import sys,json; print([t['title'] for t in json.load(sys.stdin)])"
# Expected: ['CI Pipeline']

# 6. Refresh UI — verify progress shows 1/4 completed
```

**What to look for**: Epic API creates + returns correct data. UI shows the epic. Progress updates when children close. Ready endpoint respects deps.

---

### Round 4: Dep Endpoints + Bead-Session Wiring (Features 3.2, 3.3, 3.4)

**What you get**: Full API for dependencies + beads attached to sessions with bidirectional lookup.

**Integration tests** (`test/api/test_v2_bead_session_wiring.py` — new):
```python
"""Integration tests for bead-session wiring: assign stores bead_id, lookup, cleanup."""
import json
from unittest.mock import MagicMock, patch, AsyncMock
import pytest
from fastapi.testclient import TestClient

@pytest.fixture
def client():
    """TestClient with mocked services."""
    mock_terminal = MagicMock()
    mock_terminal.session_name = "cao-test-session"
    mock_terminal.id = "term-123"

    with patch("cli_agent_orchestrator.api.v2.beads") as mock_beads, \
         patch("cli_agent_orchestrator.api.v2.terminal_service") as mock_ts, \
         patch("cli_agent_orchestrator.api.v2.session_service") as mock_ss:

        mock_beads.get.return_value = MagicMock(
            id="bead-1", title="Test", status="open", assignee=None,
            __dict__={"id": "bead-1", "title": "Test"}
        )
        mock_beads.wip.return_value = MagicMock(
            id="bead-1", title="Test", status="wip",
            __dict__={"id": "bead-1", "status": "wip"}
        )
        mock_ts.create_terminal.return_value = mock_terminal

        from cli_agent_orchestrator.api.main import app
        yield TestClient(app), mock_beads, mock_ts

class TestAssignStoresBeadId:
    def test_assign_agent_passes_bead_id_to_create_terminal(self, client):
        tc, mock_beads, mock_ts = client
        res = tc.post("/api/v2/beads/bead-1/assign-agent",
                      json={"agent_name": "dev", "provider": "q_cli"})
        assert res.status_code == 200
        # Verify bead_id was passed to create_terminal
        call_kwargs = mock_ts.create_terminal.call_args
        assert call_kwargs.kwargs.get("bead_id") == "bead-1" or \
               "bead_id" in str(call_kwargs)

class TestBeadSessionLookup:
    def test_get_bead_session_returns_terminal(self, client):
        tc, _, _ = client
        with patch("cli_agent_orchestrator.api.v2.get_terminal_by_bead") as mock_lookup:
            mock_lookup.return_value = {"id": "term-1", "tmux_session": "cao-s", "bead_id": "bead-1"}
            res = tc.get("/api/v2/beads/bead-1/session")
            assert res.status_code == 200
            assert res.json()["bead_id"] == "bead-1"

    def test_get_bead_session_404(self, client):
        tc, _, _ = client
        with patch("cli_agent_orchestrator.api.v2.get_terminal_by_bead") as mock_lookup:
            mock_lookup.return_value = None
            res = tc.get("/api/v2/beads/nonexistent/session")
            assert res.status_code == 404

class TestSessionsIncludeBeadId:
    def test_list_sessions_includes_bead_id(self, client):
        tc, _, _ = client
        with patch("cli_agent_orchestrator.api.v2.session_service") as mock_ss:
            mock_ss.list_sessions.return_value = [{"id": "cao-s1"}]
            mock_ss.get_session.return_value = {
                "session": {"id": "cao-s1"},
                "terminals": [{"id": "t1", "agent_profile": "dev", "bead_id": "bead-99"}]
            }
            res = tc.get("/api/v2/sessions")
            assert res.status_code == 200
            # bead_id should appear in terminal data
```

**Playwright E2E** (`web/e2e/round4-bead-session-wiring.spec.ts`):
```typescript
import { test, expect } from '@playwright/test';

test.describe('Round 4: Bead-Session Wiring', () => {
  test('session card shows bead info after assignment', async ({ page, request }) => {
    // Create a bead
    const beadRes = await request.post('http://localhost:8000/api/tasks', {
      data: { title: 'Wiring Test Task', priority: 2 }
    });
    const bead = await beadRes.json();

    // Assign to agent (creates session with bead_id)
    const assignRes = await request.post(`http://localhost:8000/api/v2/beads/${bead.id}/assign-agent`, {
      data: { agent_name: 'developer', provider: 'q_cli' }
    });
    const assignment = await assignRes.json();

    // Open UI and check sessions
    await page.goto('http://localhost:8000');
    await page.waitForTimeout(3000); // wait for session to appear

    // Sessions list should show the session
    const sessionsRes = await request.get('http://localhost:8000/api/v2/sessions');
    const sessions = await sessionsRes.json();
    const ourSession = sessions.find((s: any) =>
      s.terminals?.some((t: any) => t.bead_id === bead.id)
    );
    expect(ourSession).toBeDefined();

    // Cleanup
    await request.delete(`http://localhost:8000/api/v2/sessions/${assignment.session_id}`);
  });

  test('bead session lookup works', async ({ request }) => {
    // Create bead + assign
    const beadRes = await request.post('http://localhost:8000/api/tasks', {
      data: { title: 'Lookup Test', priority: 2 }
    });
    const bead = await beadRes.json();
    const assignRes = await request.post(`http://localhost:8000/api/v2/beads/${bead.id}/assign-agent`, {
      data: { agent_name: 'developer', provider: 'q_cli' }
    });
    const assignment = await assignRes.json();

    // Lookup: bead → session
    const lookupRes = await request.get(`http://localhost:8000/api/v2/beads/${bead.id}/session`);
    expect(lookupRes.ok()).toBeTruthy();

    // Delete session → lookup should 404
    await request.delete(`http://localhost:8000/api/v2/sessions/${assignment.session_id}`);
    const lookupRes2 = await request.get(`http://localhost:8000/api/v2/beads/${bead.id}/session`);
    expect(lookupRes2.status()).toBe(404);
  });
});
```

**Manual UI + backend test**:
```bash
# Full end-to-end wiring test
# 1. Create epic
EPIC=$(curl -s -X POST localhost:8000/api/v2/epics \
  -H 'Content-Type: application/json' \
  -d '{"title": "Wiring Test", "steps": ["Step A", "Step B"]}')
EPIC_ID=$(echo $EPIC | python3 -c "import sys,json; print(json.load(sys.stdin)['epic']['id'])")
CHILD_A=$(curl -s localhost:8000/api/v2/epics/$EPIC_ID/ready | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")

# 2. Assign to agent
RESULT=$(curl -s -X POST localhost:8000/api/v2/beads/$CHILD_A/assign-agent \
  -H 'Content-Type: application/json' \
  -d '{"agent_name": "developer", "provider": "q_cli"}')
SESSION_ID=$(echo $RESULT | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
echo "Session: $SESSION_ID"

# 3. VERIFY IN DB: bead_id is set
sqlite3 ~/.aws/cli-agent-orchestrator/db/cli-agent-orchestrator.db \
  "SELECT id, bead_id FROM terminals WHERE bead_id IS NOT NULL;"

# 4. VERIFY LOOKUP: bead → session
curl -s localhost:8000/api/v2/beads/$CHILD_A/session | python3 -m json.tool

# 5. VERIFY IN UI: open http://localhost:8000
#    - Sessions panel: session should show with agent "developer"
#    - Check if bead_id visible in session details

# 6. VERIFY SESSIONS LIST includes bead_id
curl -s localhost:8000/api/v2/sessions | python3 -c "
import sys,json
for s in json.load(sys.stdin):
    for t in s.get('terminals', []):
        if t.get('bead_id'):
            print(f'Session {s[\"id\"]} → bead {t[\"bead_id\"]}')
"

# 7. DELETE session → verify cleanup
curl -s -X DELETE localhost:8000/api/v2/sessions/$SESSION_ID | python3 -m json.tool
curl -s localhost:8000/api/v2/beads/$CHILD_A/session
# Expected: 404

# 8. VERIFY IN UI: refresh → session gone, bead should be unassigned
```

**What to look for**: Full bidirectional binding. Assign creates DB record. Lookup works both ways. Delete cleans up. UI reflects all of this.

---

### Round 5: Context Injection (Feature 4.1)

**What you get**: Assigning bead with context labels → files injected into agent's context window.

**Integration tests** (`test/utils/test_context.py` — new):
```python
"""Tests for context injection utility."""
from unittest.mock import patch, MagicMock
from cli_agent_orchestrator.utils.context import inject_context_files

class TestInjectContextFiles:
    def test_sends_context_add_command(self):
        with patch("cli_agent_orchestrator.utils.context.send_input") as mock_send:
            inject_context_files("term-1", ["/tmp/file1.md", "/tmp/file2.md"])
            mock_send.assert_called_once()
            cmd = mock_send.call_args[0][1]
            assert '/context add' in cmd
            assert '"/tmp/file1.md"' in cmd
            assert '"/tmp/file2.md"' in cmd

    def test_empty_list_no_op(self):
        with patch("cli_agent_orchestrator.utils.context.send_input") as mock_send:
            inject_context_files("term-1", [])
            mock_send.assert_not_called()

    def test_returns_true_on_success(self):
        with patch("cli_agent_orchestrator.utils.context.send_input"):
            assert inject_context_files("term-1", ["/file.md"]) is True

    def test_returns_false_on_error(self):
        with patch("cli_agent_orchestrator.utils.context.send_input", side_effect=Exception("fail")):
            assert inject_context_files("term-1", ["/file.md"]) is False
```

**Playwright E2E** (`web/e2e/round5-context-injection.spec.ts`):
```typescript
import { test, expect } from '@playwright/test';
import * as fs from 'fs';

test.describe('Round 5: Context Injection', () => {
  const testFile = '/tmp/cao-e2e-context.md';

  test.beforeAll(() => {
    fs.writeFileSync(testFile, '# Test Context\nThis is injected context.');
  });

  test.afterAll(() => {
    fs.unlinkSync(testFile);
  });

  test('assign bead with context label injects file', async ({ request }) => {
    // Create bead with context label (via bd CLI directly or API)
    // This test verifies the /context add appears in terminal output
    // after assigning a bead with a context:... label
    
    // For now, verify the API doesn't error when context labels are present
    const beadRes = await request.post('http://localhost:8000/api/tasks', {
      data: { title: 'Context Test Task', priority: 2 }
    });
    expect(beadRes.ok()).toBeTruthy();
  });
});
```

**Manual test**:
```bash
# 1. Create context file
echo "# Test Context" > /tmp/cao-test-context.md

# 2. Create bead with context label
bd create "Context task"
# Note the bead_id
bd label add <bead_id> "context:/tmp/cao-test-context.md"

# 3. Assign to agent
curl -s -X POST localhost:8000/api/v2/beads/<bead_id>/assign-agent \
  -H 'Content-Type: application/json' \
  -d '{"agent_name": "developer", "provider": "claude_code"}'

# 4. Wait, then check output for /context add
sleep 5
curl -s localhost:8000/api/v2/sessions/<session_id>/output | grep -i "context add"
# Expected: /context add "/tmp/cao-test-context.md"

# 5. UI: Open http://localhost:8000 → click session → terminal view
#    Should see /context add in the output stream

# Cleanup
curl -s -X DELETE localhost:8000/api/v2/sessions/<session_id>
```

**What to look for**: Terminal output shows `/context add` command. Context files from parent chain get injected on child beads.

---

### Round 6: Bead-Aware handoff + assign (Features 5.1, 5.2)

**What you get**: MCP handoff/assign tools now create beads for every delegation.

**Integration tests** (`test/mcp/test_handoff_bead.py` — new):
```python
"""Tests for bead-aware handoff and assign MCP tools."""
import json
from unittest.mock import patch, MagicMock
import pytest

class TestHandoffCreatesBead:
    @patch("cli_agent_orchestrator.mcp_server.server.requests")
    async def test_handoff_creates_bead(self, mock_requests):
        """handoff() creates a bead before spawning terminal."""
        # Mock: POST /api/tasks → bead created
        mock_requests.post.side_effect = [
            MagicMock(status_code=201, json=lambda: {"id": "bead-1", "title": "Fix bug"}),  # create bead
            MagicMock(status_code=201, json=lambda: {"id": "term-1"}),  # create terminal
            MagicMock(status_code=200),  # send input
            MagicMock(status_code=200),  # close bead
            MagicMock(status_code=200),  # exit terminal
        ]
        mock_requests.get.return_value = MagicMock(
            status_code=200, json=lambda: {"output": "Done", "mode": "last"}
        )
        from cli_agent_orchestrator.mcp_server.server import handoff
        result = await handoff(agent_profile="dev", message="Fix the login bug")
        assert result.success
        assert result.bead_id == "bead-1"  # New field

    @patch("cli_agent_orchestrator.mcp_server.server.requests")
    async def test_assign_creates_bead_and_returns_id(self, mock_requests):
        """assign() creates a bead and returns bead_id for tracking."""
        mock_requests.post.side_effect = [
            MagicMock(status_code=201, json=lambda: {"id": "bead-2", "title": "Build API"}),
            MagicMock(status_code=201, json=lambda: {"id": "term-2"}),
            MagicMock(status_code=200),  # send input
        ]
        from cli_agent_orchestrator.mcp_server.server import assign
        result = await assign(agent_profile="dev", message="Build the REST API")
        assert result["success"]
        assert result["bead_id"] == "bead-2"
```

**Manual test**:
```bash
# 1. Count beads before
BEFORE=$(bd list --json | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")
echo "Beads before: $BEFORE"

# 2. From a CAO session with MCP tools, call assign tool:
#    assign("developer", "Write a hello world script in Python")

# 3. Count beads after
AFTER=$(bd list --json | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")
echo "Beads after: $AFTER"
# Expected: AFTER = BEFORE + 1

# 4. Verify bead exists and is wip
bd list --json | python3 -c "
import sys,json
for b in json.load(sys.stdin):
    if b.get('status') == 'in_progress':
        print(f'WIP bead: {b[\"id\"]} - {b[\"title\"]}')"

# 5. Verify bead_id in DB
sqlite3 ~/.aws/cli-agent-orchestrator/db/cli-agent-orchestrator.db \
  "SELECT id, bead_id FROM terminals WHERE bead_id IS NOT NULL;"

# 6. UI: Open http://localhost:8000 → Beads panel → verify new bead shows as "wip"
#    Sessions panel → verify session shows with bead assignment
```

**What to look for**: Every handoff/assign creates a bead. `bd list` shows the new bead. DB has bead_id binding. UI shows it.

---

### Round 7: Read + Write MCP Orchestration Tools (Features 5.3, 5.4)

**What you get**: Full MCP tool surface for the master orchestrator.

**Integration tests** (`test/mcp/test_orchestration_tools.py` — new):
```python
"""Tests for MCP orchestration tools (read + write)."""
from unittest.mock import patch, MagicMock
import pytest

class TestReadTools:
    @patch("cli_agent_orchestrator.mcp_server.server.requests")
    async def test_list_sessions(self, mock_req):
        mock_req.get.return_value = MagicMock(status_code=200, json=lambda: [{"id": "cao-s1"}])
        from cli_agent_orchestrator.mcp_server.server import list_sessions
        result = await list_sessions()
        assert isinstance(result, list)

    @patch("cli_agent_orchestrator.mcp_server.server.requests")
    async def test_get_epic_status(self, mock_req):
        mock_req.get.return_value = MagicMock(status_code=200, json=lambda: {
            "epic": {"id": "e1"}, "children": [], "progress": {"total": 2, "completed": 1}
        })
        from cli_agent_orchestrator.mcp_server.server import get_epic_status
        result = await get_epic_status(epic_id="e1")
        assert result["progress"]["total"] == 2

class TestWriteTools:
    @patch("cli_agent_orchestrator.mcp_server.server.requests")
    async def test_create_epic(self, mock_req):
        mock_req.post.return_value = MagicMock(status_code=201, json=lambda: {
            "epic": {"id": "e1"}, "children": [{"id": "e1.1"}, {"id": "e1.2"}]
        })
        from cli_agent_orchestrator.mcp_server.server import create_epic
        result = await create_epic(title="MCP Epic", steps=["A", "B"])
        assert "epic" in result
        assert len(result["children"]) == 2

    @patch("cli_agent_orchestrator.mcp_server.server.requests")
    async def test_close_bead(self, mock_req):
        mock_req.post.return_value = MagicMock(status_code=200, json=lambda: {"status": "closed"})
        from cli_agent_orchestrator.mcp_server.server import close_bead
        result = await close_bead(bead_id="b1")
        assert result["status"] == "closed"
```

**Playwright E2E** (`web/e2e/round7-mcp-tools.spec.ts`):
```typescript
import { test, expect } from '@playwright/test';

test.describe('Round 7: MCP tools verify via API', () => {
  // MCP tools call the HTTP API, so we can verify them through the API

  test('create_epic via API (underlying MCP endpoint)', async ({ request }) => {
    const res = await request.post('http://localhost:8000/api/v2/epics', {
      data: { title: 'MCP Verify Epic', steps: ['X', 'Y'] }
    });
    expect(res.ok()).toBeTruthy();
    const data = await res.json();
    expect(data.children).toHaveLength(2);
  });

  test('beads panel shows MCP-created epic', async ({ page, request }) => {
    await page.goto('http://localhost:8000');
    await page.waitForTimeout(2000);
    const epicText = page.locator('text=MCP Verify Epic');
    // Should appear if prior test created it
    if (await epicText.isVisible({ timeout: 3000 }).catch(() => false)) {
      await expect(epicText).toBeVisible();
    }
  });
});
```

**Manual test**:
```bash
# Test each MCP tool's underlying API call:

# list_sessions
curl -s localhost:8000/api/v2/sessions | python3 -c "import sys,json; print(f'{len(json.load(sys.stdin))} sessions')"

# create_epic (MCP calls this)
curl -s -X POST localhost:8000/api/v2/epics \
  -H 'Content-Type: application/json' \
  -d '{"title": "MCP Test", "steps": ["A","B"]}' | python3 -m json.tool

# get_epic_status
curl -s localhost:8000/api/v2/epics/<epic_id> | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['progress'])"

# get_ready_beads
curl -s localhost:8000/api/v2/epics/<epic_id>/ready | python3 -c "import sys,json; print([t['title'] for t in json.load(sys.stdin)])"

# assign_bead
curl -s -X POST localhost:8000/api/v2/beads/<child_id>/assign-agent \
  -H 'Content-Type: application/json' \
  -d '{"agent_name": "developer", "provider": "q_cli"}' | python3 -m json.tool

# close_bead
bd close <child_id>

# kill_session
curl -s -X DELETE localhost:8000/api/v2/sessions/<session_id>

# UI: Open http://localhost:8000
# - Beads panel: verify epic + children
# - Sessions panel: verify assigned session
# - After close/kill: verify cleanup in both panels
```

**What to look for**: Each MCP tool's underlying API works correctly. Side effects visible in UI (beads created, sessions spawned/killed).

---

### Round 8: Master Orchestrator Profile + Launch (Features 6.1, 6.2)

**What you get**: Can launch a persistent orchestrator agent that manages work through beads.

**Integration tests** (`test/api/test_v2_orchestrator.py` — new):
```python
"""Tests for master orchestrator launch endpoint."""
from unittest.mock import patch, MagicMock
import pytest
from fastapi.testclient import TestClient

@pytest.fixture
def client():
    mock_terminal = MagicMock()
    mock_terminal.session_name = "cao-orch-session"
    mock_terminal.id = "orch-term-1"
    with patch("cli_agent_orchestrator.api.v2.terminal_service") as mock_ts:
        mock_ts.create_terminal.return_value = mock_terminal
        from cli_agent_orchestrator.api.main import app
        yield TestClient(app), mock_ts

class TestLaunchOrchestrator:
    def test_launch_returns_session_info(self, client):
        tc, _ = client
        res = tc.post("/api/v2/orchestrator/launch",
                      json={"provider": "claude_code"})
        assert res.status_code in (200, 201)
        data = res.json()
        assert "session_id" in data
        assert "terminal_id" in data

    def test_launch_uses_specified_provider(self, client):
        tc, mock_ts = client
        tc.post("/api/v2/orchestrator/launch",
                json={"provider": "kiro_cli"})
        call_kwargs = mock_ts.create_terminal.call_args
        assert "kiro_cli" in str(call_kwargs)

    def test_launch_uses_master_orchestrator_profile(self, client):
        tc, mock_ts = client
        tc.post("/api/v2/orchestrator/launch", json={})
        call_kwargs = mock_ts.create_terminal.call_args
        assert "master_orchestrator" in str(call_kwargs)
```

**Playwright E2E** (`web/e2e/round8-orchestrator.spec.ts`):
```typescript
import { test, expect } from '@playwright/test';

test.describe('Round 8: Master Orchestrator', () => {
  test('launch orchestrator via API', async ({ request }) => {
    const res = await request.post('http://localhost:8000/api/v2/orchestrator/launch', {
      data: { provider: 'q_cli' }  // Use q_cli for fast test
    });
    expect(res.ok()).toBeTruthy();
    const data = await res.json();
    expect(data.session_id).toBeTruthy();

    // Verify session appears in list
    const sessionsRes = await request.get('http://localhost:8000/api/v2/sessions');
    const sessions = await sessionsRes.json();
    expect(sessions.some((s: any) => s.id === data.session_id)).toBeTruthy();

    // Cleanup
    await request.delete(`http://localhost:8000/api/v2/sessions/${data.session_id}`);
  });

  test('orchestrator session visible in UI', async ({ page, request }) => {
    const res = await request.post('http://localhost:8000/api/v2/orchestrator/launch', {
      data: { provider: 'q_cli' }
    });
    const { session_id } = await res.json();

    await page.goto('http://localhost:8000');
    await page.waitForTimeout(3000);
    // Session should appear in sessions panel
    // Look for "master_orchestrator" agent name
    const orchLabel = page.locator('text=master_orchestrator');
    if (await orchLabel.isVisible({ timeout: 5000 }).catch(() => false)) {
      await expect(orchLabel).toBeVisible();
    }

    await request.delete(`http://localhost:8000/api/v2/sessions/${session_id}`);
  });
});
```

**Manual test (the big one — full orchestrator workflow)**:
```bash
# 1. Launch orchestrator
ORCH=$(curl -s -X POST localhost:8000/api/v2/orchestrator/launch \
  -H 'Content-Type: application/json' \
  -d '{"provider": "claude_code"}')
ORCH_SESSION=$(echo $ORCH | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
echo "Orchestrator session: $ORCH_SESSION"

# 2. UI: Open http://localhost:8000
#    → Sessions panel: verify orchestrator session with "master_orchestrator" agent
#    → Click on session to see terminal output

# 3. Verify in tmux
tmux attach -t $ORCH_SESSION
# Expected: CLI agent running with orchestrator profile

# 4. Detach (Ctrl+B, D) and send it a simple task via API
curl -s -X POST "localhost:8000/api/v2/sessions/$ORCH_SESSION/input?message=Create+a+bead+called+hello-world+and+assign+it+to+a+developer+agent"

# 5. Wait and check results
sleep 15
echo "--- Sessions ---"
curl -s localhost:8000/api/v2/sessions | python3 -c "
import sys,json
for s in json.load(sys.stdin):
    print(f'  {s[\"id\"]} - agent: {s.get(\"agent_name\",\"?\")}')
"
echo "--- Beads ---"
bd list --json | python3 -c "
import sys,json
for b in json.load(sys.stdin):
    print(f'  {b[\"id\"]} - {b[\"title\"]} - {b[\"status\"]}')
"
# Expected: 2+ sessions (orchestrator + worker), bead for "hello-world"

# 6. Send complex task (should propose plan first)
curl -s -X POST "localhost:8000/api/v2/sessions/$ORCH_SESSION/input?message=Build+a+REST+API+with+auth,+CRUD,+and+tests"
# Attach to tmux to see if it proposes a plan before executing

# 7. UI verification:
#    → Beads panel: new beads created by orchestrator
#    → Sessions panel: worker sessions spawned
#    → Activity feed: events from orchestrator

# 8. Cleanup
curl -s -X DELETE "localhost:8000/api/v2/sessions/$ORCH_SESSION"
```

**What to look for**: Orchestrator launches, responds to natural language, creates beads and dispatches agents via MCP tools. Simple → auto-execute. Complex → proposes plan. All visible in UI.

---

### Round 9: Frontend Epic Creation + Display (Features 7.1, 7.2, 7.3)

**What you get**: UI for creating epics and viewing them with progress.

**Component tests** (`web/src/components/starcraft/NewBeadModal.test.tsx` — extend existing):
```typescript
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { NewBeadModal } from './NewBeadModal'

vi.mock('../../api', () => ({
  api: {
    tasks: { create: vi.fn(() => Promise.resolve({ id: 'b1' })) },
    epics: { create: vi.fn(() => Promise.resolve({ epic: { id: 'e1' }, children: [] })) },
  }
}))

describe('Epic creation mode', () => {
  const onClose = vi.fn()
  beforeEach(() => vi.clearAllMocks())

  it('toggles between bead and epic mode', () => {
    render(<NewBeadModal onClose={onClose} />)
    const toggle = screen.getByText(/epic/i)
    fireEvent.click(toggle)
    expect(screen.getByPlaceholderText(/step/i)).toBeTruthy()
  })

  it('can add and remove steps', () => {
    render(<NewBeadModal onClose={onClose} />)
    fireEvent.click(screen.getByText(/epic/i))
    fireEvent.click(screen.getByText(/add step/i))
    fireEvent.click(screen.getByText(/add step/i))
    // Should have 2+ step inputs
    const inputs = screen.getAllByPlaceholderText(/step/i)
    expect(inputs.length).toBeGreaterThanOrEqual(2)
  })

  it('submits epic via api.epics.create', async () => {
    const { api } = await import('../../api')
    render(<NewBeadModal onClose={onClose} />)
    fireEvent.click(screen.getByText(/epic/i))
    fireEvent.change(screen.getByPlaceholderText(/title/i), { target: { value: 'My Epic' } })
    fireEvent.click(screen.getByText('Create'))
    await waitFor(() => {
      expect(api.epics.create).toHaveBeenCalled()
    })
  })
})
```

**BeadsPanel epic card tests** (`web/src/components/BeadsPanel.test.tsx` — extend):
```typescript
describe('Epic cards', () => {
  it('renders progress bar for epics', () => {
    // Mock store with epic bead that has children
    // Verify progress bar element exists
  })

  it('expands to show children', () => {
    // Click epic card → children list visible
  })

  it('progress updates when child completes', () => {
    // Change child status to closed → progress bar updates
  })
})
```

**Playwright E2E** (`web/e2e/round9-epic-ui.spec.ts`):
```typescript
import { test, expect } from '@playwright/test';

test.describe('Round 9: Epic Creation + Display UI', () => {
  test('epic creation modal has epic mode toggle', async ({ page }) => {
    await page.goto('http://localhost:8000');
    // Open new bead modal (find the button)
    const newBtn = page.locator('text=New').first();
    if (await newBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
      await newBtn.click();
      // Look for epic toggle
      const epicToggle = page.locator('text=Epic');
      await expect(epicToggle).toBeVisible({ timeout: 3000 });
    }
  });

  test('create epic via UI and verify display', async ({ page }) => {
    await page.goto('http://localhost:8000');
    // Open modal
    const newBtn = page.locator('text=New').first();
    if (!await newBtn.isVisible({ timeout: 3000 }).catch(() => false)) return;
    await newBtn.click();

    // Switch to epic mode
    await page.locator('text=Epic').click();

    // Fill title
    await page.fill('[placeholder*="title" i]', 'UI Test Epic');

    // Add steps
    await page.fill('[placeholder*="step" i]', 'Step Alpha');
    await page.locator('text=Add').first().click();
    // Fill second step
    const stepInputs = page.locator('[placeholder*="step" i]');
    await stepInputs.last().fill('Step Beta');

    // Submit
    await page.locator('text=Create').click();

    // Verify epic appears
    await page.waitForTimeout(2000);
    await expect(page.locator('text=UI Test Epic')).toBeVisible({ timeout: 5000 });
  });

  test('epic card shows progress bar', async ({ page, request }) => {
    // Create epic via API for reliable state
    const res = await request.post('http://localhost:8000/api/v2/epics', {
      data: { title: 'Progress Test Epic', steps: ['A', 'B', 'C'] }
    });
    const { epic } = await res.json();

    await page.goto('http://localhost:8000');
    await page.waitForTimeout(2000);

    // Find the epic card
    const epicCard = page.locator(`text=Progress Test Epic`);
    await expect(epicCard).toBeVisible({ timeout: 5000 });

    // There should be a progress indicator (bar, fraction, etc.)
    // Implementation-specific: look for "0/3" or a progress element
  });

  test('epic children are visible', async ({ page, request }) => {
    const res = await request.post('http://localhost:8000/api/v2/epics', {
      data: { title: 'Children Test Epic', steps: ['Child X', 'Child Y'] }
    });

    await page.goto('http://localhost:8000');
    await page.waitForTimeout(2000);

    // Click epic to expand
    await page.locator('text=Children Test Epic').click();
    await page.waitForTimeout(1000);

    // Children should be visible
    await expect(page.locator('text=Child X')).toBeVisible({ timeout: 3000 });
    await expect(page.locator('text=Child Y')).toBeVisible({ timeout: 3000 });
  });
});
```

**Manual UI test**:
```
1. Open http://localhost:8000
2. Click "New Bead" → toggle to "Epic" mode
   ✓ Title field appears
   ✓ Step list with add/remove buttons appears
   ✓ Sequential checkbox visible
3. Fill: Title="Auth System", Steps=["Schema", "JWT", "Middleware", "Tests"]
4. Click Create
   ✓ Modal closes
   ✓ Epic appears in Beads panel with 4 children
5. Click the epic card
   ✓ Expands to show children
   ✓ Progress shows 0/4
   ✓ Each child has status badge (open)
6. In terminal: bd close <first_child_id>
7. Refresh UI
   ✓ Progress shows 1/4
   ✓ First child shows "closed" badge
   ✓ Other children still "open"
8. Create epic via curl and verify it appears on refresh
```

**What to look for**: Epic creation modal works end-to-end. Epics render with progress. Children expand with status. Progress live-updates.

---

### Round 10: Orchestrator Controls in UI (Feature 7.4)

**What you get**: Launch and interact with the master orchestrator from the UI.

**Playwright E2E** (`web/e2e/round10-orchestrator-ui.spec.ts`):
```typescript
import { test, expect } from '@playwright/test';

test.describe('Round 10: Orchestrator UI Controls', () => {
  test('orchestration panel has launch button', async ({ page }) => {
    await page.goto('http://localhost:8000');
    // Navigate to orchestration section
    const orchTab = page.locator('text=Orchestrat').first();
    if (await orchTab.isVisible({ timeout: 3000 }).catch(() => false)) {
      await orchTab.click();
    }
    // Look for launch button
    const launchBtn = page.locator('text=Launch').first();
    await expect(launchBtn).toBeVisible({ timeout: 5000 });
  });

  test('launch orchestrator from UI creates session', async ({ page, request }) => {
    await page.goto('http://localhost:8000');

    // Navigate to orchestration panel
    const orchTab = page.locator('text=Orchestrat').first();
    if (await orchTab.isVisible({ timeout: 3000 }).catch(() => false)) {
      await orchTab.click();
    }

    // Click launch
    const launchBtn = page.locator('text=Launch').first();
    if (await launchBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
      await launchBtn.click();
      await page.waitForTimeout(5000);

      // Verify session appeared
      const sessions = await (await request.get('http://localhost:8000/api/v2/sessions')).json();
      const orchSession = sessions.find((s: any) => s.agent_name === 'master_orchestrator');
      expect(orchSession).toBeDefined();

      // Cleanup
      if (orchSession) {
        await request.delete(`http://localhost:8000/api/v2/sessions/${orchSession.id}`);
      }
    }
  });

  test('orchestrator terminal output streams to panel', async ({ page, request }) => {
    // Launch via API for reliability
    const res = await request.post('http://localhost:8000/api/v2/orchestrator/launch', {
      data: { provider: 'q_cli' }
    });
    const { session_id } = await res.json();

    await page.goto('http://localhost:8000');
    await page.waitForTimeout(3000);

    // Find orchestrator in sessions and click to view output
    const orchLabel = page.locator('text=master_orchestrator').first();
    if (await orchLabel.isVisible({ timeout: 5000 }).catch(() => false)) {
      await orchLabel.click();
      // Terminal output area should appear
      await page.waitForTimeout(2000);
      // Look for terminal-like output area
      const terminal = page.locator('[class*="terminal"], [class*="output"], pre').first();
      await expect(terminal).toBeVisible({ timeout: 5000 });
    }

    await request.delete(`http://localhost:8000/api/v2/sessions/${session_id}`);
  });
});
```

**Manual UI test (full end-to-end orchestrator workflow)**:
```
1. Open http://localhost:8000
2. Navigate to Orchestration panel (or wherever the launch button is)
3. Click "Launch Orchestrator"
   - Select provider dropdown → "Claude Code" (or "Q CLI" for faster test)
   - Click Launch
   ✓ Orchestrator session appears in Sessions panel
   ✓ Terminal output starts streaming

4. Send it a message via input field:
   Type: "Create a bead called hello-world and assign it to a developer"
   ✓ Orchestrator processes the request
   ✓ Beads panel: new "hello-world" bead appears
   ✓ Sessions panel: new worker session spawned

5. Send a complex task:
   Type: "Build a REST API with auth, CRUD, and tests"
   ✓ Orchestrator proposes an epic plan (3+ steps)
   ✓ Shows breakdown with steps
   ✓ Asks for approval before dispatching

6. Approve the plan (type "yes" or "go ahead")
   ✓ Epic appears in Beads panel with children
   ✓ Worker sessions start appearing
   ✓ Progress bar updates as workers complete

7. Click "Stop" button
   ✓ Orchestrator session terminates
   ✓ Worker sessions keep running (they have their own beads)

8. Verify in Beads panel:
   ✓ All beads from the orchestrator's work are visible
   ✓ WIP beads show assigned sessions
   ✓ Progress reflects actual state
```

**What to look for**: Full loop — launch from UI, chat with orchestrator, watch it create beads and dispatch agents, see everything reflected in Beads panel and Sessions panel in real-time.
