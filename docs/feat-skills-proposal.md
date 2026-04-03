# Skills Support for CAO

## Problem

CAO agents receive their instructions through agent profiles — monolithic Markdown files that define who the agent is and how it should behave. When multiple agents need the same domain knowledge (e.g., testing conventions, deployment procedures, coding standards), that knowledge must be duplicated across each agent profile or left out entirely.

There is no mechanism for reusable, composable blocks of knowledge that can be shared across agents. This leads to bloated profiles, duplicated instructions, and no clear separation between agent identity (who the agent is) and agent capability (what the agent knows how to do).

Additionally, injecting all instructional content into the system prompt at launch wastes context window budget. An agent working on a database migration doesn't need testing conventions loaded upfront — but today, any knowledge the agent might need must be baked into the profile from the start.

## Scope

Skills are a CAO-native feature for loading reusable instructional content into agent context on demand. The following boundaries define what skills are and are not:

- **Provider agnostic.** Skills are not tied to any specific CLI tool provider. They are accessed through the CAO MCP server, which all providers already integrate with.
- **CAO-only.** Skills are intended exclusively for agents running within CAO orchestration. They do not conflict with or replace provider-native skill systems (e.g., Claude Code's skill directories). Provider-defined skills should continue to live in their respective provider skill directories for use outside of CAO context.
- **Instructional content only.** A skill is a block of Markdown text — domain knowledge, conventions, procedures, guidelines. Skills do not carry tool permissions, MCP server configurations, or other structured capabilities.
- **Statically declared, lazily loaded.** Available skills are defined in the agent profile frontmatter at creation time. Skill content is retrieved on demand by the agent via an MCP tool call, not injected into the system prompt at launch.

## Proposed Solution

### Skill files

A skill is a folder containing a `SKILL.md` file at minimum. The folder name must match the `name` field in the `SKILL.md` frontmatter. The body contains the full skill content.

All skills live in a single user-writable location: `~/.aws/cli-agent-orchestrator/skills/`. Default skills are shipped with the CAO package at `src/cli_agent_orchestrator/skills/` and seeded into the user store during `cao init`. There is no separation between built-in and user-installed skills — users can freely edit any skill, including defaults.

### Agent profile integration

Agent profiles gain a `skills` field in their YAML frontmatter — a list of skill names that the agent has access to.

### Skill loading at terminal creation

When the server creates a terminal for an agent with skills declared in its profile, it reads the skill metadata (name and description) directly from the filesystem. There is no persistent cache or database table — the skill store directory is the single source of truth, read on demand. If any declared skill is missing from the filesystem, terminal creation fails with an error.

### Context injection at launch

When an agent is launched with skills listed in its profile, CAO appends a short block to the system prompt. This block lists each available skill by name and description, and instructs the agent to use the `get_skill` MCP tool to retrieve full content when needed.

The agent profile body can also explicitly instruct the agent to load specific skills eagerly (e.g., "Before starting, load the python-testing skill"). Otherwise, the agent uses the description to decide on its own whether and when to retrieve a skill.

### MCP tool

The `cao-mcp-server` exposes a new `get_skill` tool. It accepts a skill name and returns the skill's Markdown body. No `list_skills` tool is needed — the injected prompt already tells the agent what skills are available and what each one does.

## Why This Matters

### Context efficiency

Most orchestration systems inject all instructional content upfront, consuming context window budget regardless of whether the agent needs it. The lazy-loading approach means skill content only enters the agent's context when the agent decides it's relevant. An agent handling a simple file rename doesn't need to load a 2,000-word database migration guide just because it's listed in the profile.

### Reusability across agents and profiles

Today, if three different agent profiles need the same coding standards, those standards are copied into each profile. Skills allow that knowledge to be defined once and referenced from any profile. When the standards change, one file is updated instead of three.

### Separation of identity and capability

Agent profiles should define who the agent is — its role, personality, MCP servers, tool permissions. Skills define what the agent knows how to do. This separation makes profiles cleaner and skills independently composable.

### Enabling domain-specific agent teams

Consider a team of CAO agents working on a Python project: a developer, a reviewer, and a supervisor. The developer and reviewer both need knowledge of the project's testing conventions and code style, but they use that knowledge differently. With skills, both profiles reference the same `python-testing` and `code-style` skills, while their profile bodies define how each role applies that knowledge. Adding a new `security-review` skill later doesn't require rewriting any profiles — just adding the skill name to the relevant frontmatter lists.

### User-extensible without forking

Users can author custom skills in `~/.aws/cli-agent-orchestrator/skills/` tailored to their organization's standards, internal tooling, or domain-specific workflows. These skills work alongside built-in skills with no additional configuration.

## Example Use Case

### Packaging multi-agent communication primitives

CAO's multi-agent orchestration relies on agents understanding communication protocols — how `assign` and `handoff` differ, when to use `send_message`, how message delivery works with idle detection, and how to parse callback terminal IDs from task messages. Today, this knowledge is duplicated across every agent profile that participates in multi-agent workflows.

The existing `examples/assign/` profiles illustrate this: `analysis_supervisor.md` dedicates a full section to explaining how `assign`, `handoff`, and `send_message` work, plus a "How Message Delivery Works" section on idle-based delivery. `data_analyst.md` repeats the `send_message` tool description and callback workflow. `report_generator.md` explains handoff return semantics. The built-in `developer.md` profile has its own "Multi-Agent Communication" section covering the same handoff vs. assign distinction. Each profile re-teaches the same communication patterns in slightly different words.

With skills, these primitives become shared skills — e.g., a `cao-supervisor-protocols` skill covering assign/handoff orchestration and idle-based message delivery, and a `cao-worker-protocols` skill covering callback patterns and send_message usage. Domain-specific profiles like `data_analyst` or `report_generator` would then focus purely on their domain (statistical analysis, report formatting) and declare the appropriate communication skill in their frontmatter. When CAO's communication semantics evolve — say, a new orchestration mode is added — the skill is updated once rather than patching every agent profile that participates in multi-agent workflows.

## Technical Design: Skill Installation

This section covers the technical design for skill installation, removal, and discovery via the `cao skills` CLI and the `cao init` seeding mechanism.

### Skill file structure

A skill is a folder containing a `SKILL.md` file at minimum. The folder name must match the `name` field in the `SKILL.md` frontmatter.

```
python-testing/
└── SKILL.md
```

`SKILL.md` uses YAML frontmatter with two required fields:

```markdown
---
name: python-testing
description: Python testing conventions using pytest, fixtures, and coverage requirements
---

# Python Testing Conventions

Use pytest for all test files...
```

### Skill store

All skills live in a single user-writable location:

```
~/.aws/cli-agent-orchestrator/skills/
├── cao-supervisor-protocols/
│   └── SKILL.md
├── cao-worker-protocols/
│   └── SKILL.md
└── python-testing/
    └── SKILL.md
```

There is no separation between built-in and user-installed skills. Default skills are seeded into this directory during `cao init` and can be freely edited by the user.

Default skills are shipped with the CAO package at `src/cli_agent_orchestrator/skills/` and serve as the source for seeding.

### CLI commands

#### `cao skills add <folder-path>`

Installs a skill from a local folder path into the skill store.

**Validation steps (in order):**

1. Verify `<folder-path>` is a directory
2. Verify the directory contains a `SKILL.md` file
3. Parse `SKILL.md` frontmatter and verify `name` and `description` are present and non-empty
4. Verify the folder name matches the frontmatter `name`
5. Verify the name passes path traversal checks (no `/` or `..`)
6. Verify a skill with the same name does not already exist in the store (unless `--force` is passed)

**On success:** copy the entire skill folder to `~/.aws/cli-agent-orchestrator/skills/{name}/`.

**Flags:**

| Flag | Behavior |
|------|----------|
| `--force` | Overwrite an existing skill with the same name |

#### `cao skills remove <name>`

Removes an installed skill from the skill store.

1. Validate the name passes path traversal checks
2. Verify `~/.aws/cli-agent-orchestrator/skills/{name}/` exists
3. Remove the skill folder

#### `cao skills list`

Lists all installed skills.

1. Scan `~/.aws/cli-agent-orchestrator/skills/*/SKILL.md`
2. Parse frontmatter from each `SKILL.md`
3. Display a table with `name` and `description` columns

### Default skill seeding via `cao init`

`cao init` currently initializes the SQLite database. It gains additional responsibility for seeding default skills into the user store.

**Seeding behavior:**

1. Create `~/.aws/cli-agent-orchestrator/skills/` if it does not exist
2. For each default skill folder in `src/cli_agent_orchestrator/skills/`:
   - If a skill with the same name already exists in the user store, **skip it** (preserves user edits)
   - Otherwise, copy the skill folder to the user store

This means `cao init` is safe to re-run — it will seed any new default skills added in package updates without overwriting skills the user has modified.

### Design decisions

| Decision | Rationale |
|----------|-----------|
| Single store (no built-in vs custom split) | Users should be able to edit default skills. Separating stores would require shadowing logic and complicate the mental model. |
| Folder-based structure | A skill folder can contain additional files beyond `SKILL.md` in the future (e.g., example snippets, templates) without changing the installation format. |
| Folder name must match frontmatter `name` | Prevents filename/name mismatches and makes `cao skills remove <name>` predictable. |
| Frontmatter `name` is authoritative | The name in the frontmatter is the skill's identity. The folder name is a structural requirement that must agree with it. |
| `cao init` skips existing skills | Prevents accidental overwrite of user edits when re-running init or upgrading CAO. |
| `--force` required for overwrite on add | Protects against accidental overwrites, especially of edited default skills. |
| File path source only | URL and registry support can be added later. Keeping the initial scope minimal reduces complexity. |
| No body validation | Skill content is freeform Markdown. The author is responsible for content quality. |

### Remaining technical design

All technical design areas are now covered in subsequent sections of this document.

## Technical Design: Server-Side Skill Loading

This section covers how the CAO server loads skill metadata at terminal creation time and how the skill utility module is structured.

### Design approach: filesystem as source of truth

The skill store directory (`~/.aws/cli-agent-orchestrator/skills/`) is the single source of truth for skill data. There is no database table, in-memory cache, or server-side metadata store. The server reads skill metadata directly from the filesystem each time it needs it.

**Rationale:**

| Approach considered | Why not |
|---------------------|---------|
| Database table | Introduces a filesystem-DB sync problem. Users can edit `SKILL.md` directly, which would leave the DB stale until the next server restart. Nothing else in the system has this sync obligation today. |
| In-memory cache | Adds complexity for multi-process or future scale-out scenarios. Requires cache invalidation on skill add/remove/edit. |
| Filesystem reads | Skills are small, few in number, and read only at terminal creation time — an infrequent operation. The performance cost of parsing YAML frontmatter from a handful of files is negligible. |

### Skill utility module: `utils/skills.py`

A new utility module at `src/cli_agent_orchestrator/utils/skills.py` provides skill loading functions, analogous to `utils/agent_profiles.py` for agent profiles.

#### `load_skill_metadata(name: str) -> SkillMetadata`

Loads the metadata (name and description) for a single skill.

1. Validate the name passes path traversal checks (no `/` or `..`)
2. Resolve the path: `~/.aws/cli-agent-orchestrator/skills/{name}/SKILL.md`
3. Read the file and parse YAML frontmatter
4. Validate `name` and `description` are present and non-empty
5. Validate the folder name matches the frontmatter `name`
6. Return a `SkillMetadata` object

Raises an error if the skill folder does not exist, `SKILL.md` is missing, or frontmatter validation fails.

#### `load_skill_content(name: str) -> str`

Loads the full Markdown body of a skill (for use by the `get_skill` MCP tool).

1. Validate and resolve path (same as `load_skill_metadata`)
2. Read the file and parse frontmatter
3. Return the Markdown body content

#### `list_skills() -> list[SkillMetadata]`

Lists all installed skills.

1. Scan `~/.aws/cli-agent-orchestrator/skills/*/SKILL.md`
2. For each valid skill folder, call `load_skill_metadata`
3. Skip folders with invalid structure (missing `SKILL.md`, bad frontmatter) and log a warning
4. Return the list sorted by name

#### `validate_skill_folder(path: Path) -> SkillMetadata`

Validates a skill folder at an arbitrary path (used by `cao skills add` before copying to the store).

1. Verify the path is a directory
2. Verify it contains a `SKILL.md` file
3. Parse and validate frontmatter (`name` and `description` required, non-empty)
4. Verify folder name matches frontmatter `name`
5. Return a `SkillMetadata` object on success, raise on failure

### Skill metadata model

A lightweight Pydantic model at `src/cli_agent_orchestrator/models/skill.py`:

```python
class SkillMetadata(BaseModel):
    name: str
    description: str
```

### Skill loading during terminal creation

When the server creates a terminal (via `handoff`, `assign`, or direct API call), the terminal creation flow gains a skill resolution step:

1. Load the agent profile (existing step)
2. If the profile has a `skills` list:
   a. For each skill name, call `load_skill_metadata(name)`
   b. If any skill is missing or invalid, **fail terminal creation with an error**
   c. Build the skill catalog injection block (list of skill names and descriptions)
   d. Append the injection block to the system prompt

**Fail-loud behavior:** If an agent profile declares a skill that does not exist on the filesystem, terminal creation fails immediately with a clear error message identifying the missing skill. This prevents agents from running with missing context they were expected to have.

### Integration points

| Component | Change |
|-----------|--------|
| `utils/skills.py` | New module — skill loading, validation, and listing |
| `models/skill.py` | New model — `SkillMetadata` |
| `constants.py` | New constant — `SKILLS_DIR = CAO_HOME_DIR / "skills"` |
| Provider `build_command` methods | Call skill loading utilities, append skill catalog to system prompt |
| `cao skills` CLI commands | Use `validate_skill_folder`, `list_skills` from the utility module |
| `cao init` | Use `SKILLS_DIR` constant for seeding default skills |

### Design decisions

| Decision | Rationale |
|----------|-----------|
| No persistent store | The filesystem is the source of truth. Avoids sync complexity between a cache/DB and user-editable files. |
| Fail on missing skill | An agent declared with skills expects that context. Running without it risks incorrect behavior that is harder to debug than a clear startup error. |
| `utils/skills.py` as utility module | Follows the existing pattern of `utils/agent_profiles.py`. Keeps skill logic reusable across CLI commands, server, and MCP tool. |
| Warnings for invalid skills in `list_skills` | `list_skills` is a discovery operation — a single malformed skill should not prevent listing the rest. Contrast with `load_skill_metadata` which is strict because it serves a specific request. |

## Technical Design: Skill Body Retrieval

This section covers how agents retrieve full skill content at runtime via the `get_skill` MCP tool and the backing `cao-server` HTTP endpoint.

### Request flow

```
Agent calls get_skill MCP tool
  → cao-mcp-server calls GET /skills/{name} on cao-server
    → cao-server calls load_skill_content(name) from utils/skills.py
      → reads ~/.aws/cli-agent-orchestrator/skills/{name}/SKILL.md
    → returns skill body or HTTP error
  → MCP tool returns content or error message to agent
```

### cao-server HTTP endpoint

#### `GET /skills/{name}`

Returns the full Markdown body of a skill.

**Path parameter:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `name` | string | Skill name (must match folder name and frontmatter `name`) |

**Response:**

```json
{
  "name": "python-testing",
  "content": "# Python Testing Conventions\n\nUse pytest for all test files..."
}
```

**Error responses:**

| Status | Condition | Detail |
|--------|-----------|--------|
| 400 | Invalid name (path traversal) | `"Invalid skill name: {name}"` |
| 404 | Skill folder or `SKILL.md` not found | `"Skill not found: {name}"` |
| 500 | Filesystem read error or frontmatter parse failure | `"Failed to load skill: {error}"` |

**Implementation:** The endpoint calls `load_skill_content(name)` from `utils/skills.py`. Path traversal validation and frontmatter parsing are handled by the utility function. The endpoint maps exceptions to the appropriate HTTP status codes, following the same pattern used by existing endpoints like `GET /flows/{name}` and `GET /terminals/{terminal_id}`.

### cao-mcp-server `get_skill` tool

The MCP server exposes a `get_skill` tool that agents call to retrieve skill content.

```python
@mcp.tool()
async def get_skill(
    name: str = Field(description="Name of the skill to retrieve"),
) -> Dict[str, Any]:
```

**Behavior:**

1. Call `GET /skills/{name}` on the cao-server HTTP API
2. On success (200): return the skill body content to the agent
3. On error (4xx/5xx): return an error dict with the detail message

**Error handling follows existing MCP patterns.** The `handoff`, `assign`, and `send_message` tools all wrap API calls in try/except and return structured results. `get_skill` does the same:

- HTTP errors surface the server's error detail (e.g., "Skill not found: foo-bar")
- Connection errors surface a message indicating the cao-server may not be running

### Integration points

| Component | Change |
|-----------|--------|
| `api/main.py` | New `GET /skills/{name}` endpoint |
| `mcp_server/server.py` | New `get_skill` tool registration |
| `utils/skills.py` | `load_skill_content` (already designed in previous section) |

### Design decisions

| Decision | Rationale |
|----------|-----------|
| Path parameter (`/skills/{name}`) | Consistent with existing resource endpoints (`/flows/{name}`, `/terminals/{id}`). Produces clear 404s and leaves `GET /skills` available for listing. |
| No `list_skills` MCP tool | The agent already knows its available skills from the catalog injected into its system prompt at launch. A listing tool would be redundant. |
| MCP tool returns error messages, not exceptions | Agents should receive actionable error text they can reason about (e.g., "Skill not found: foo-bar") rather than opaque failures. This matches how `handoff` returns `HandoffResult(success=False, message=...)`. |
| Server reads from filesystem on each request | No cache to invalidate. If a user edits a skill while the server is running, the next `get_skill` call returns the updated content immediately. |

## Technical Design: Agent Profile Integration and Context Injection

This section covers the `skills` field in agent profile frontmatter and the injection logic that appends a skill catalog to the agent's system prompt at terminal creation time.

### Agent profile `skills` field

The `AgentProfile` Pydantic model gains a new optional field:

```python
skills: Optional[List[str]] = None
```

An agent profile with skills declared:

```yaml
---
name: developer
description: Developer Agent
role: developer
skills:
  - cao-worker-protocols
  - python-testing
mcpServers:
  cao-mcp-server:
    type: stdio
    command: uvx
    args:
      - "--from"
      - "git+https://github.com/awslabs/cli-agent-orchestrator.git@main"
      - "cao-mcp-server"
---

# DEVELOPER AGENT

You are the Developer Agent...
```

No validation on the model itself beyond type checking. Skill existence is validated at terminal creation time, not at profile parse time. This keeps profile loading fast and decoupled from the filesystem.

### Injection point: `terminal_service.create_terminal`

Skill resolution and prompt injection happen in `terminal_service.create_terminal`, before the provider is initialized. This centralizes the logic and keeps providers skill-unaware.

**Modified terminal creation flow:**

1. Generate terminal identifiers (existing)
2. Create tmux session/window (existing)
3. Save terminal metadata to database (existing)
4. Resolve allowed tools (existing)
5. **Load agent profile and resolve skills (new)**
   a. Load the agent profile
   b. If the profile has a `skills` list:
      - For each skill name, call `load_skill_metadata(name)` from `utils/skills.py`
      - If any skill is missing or invalid, **fail terminal creation with an error**
      - Build the skill catalog block
      - Append the catalog block to `profile.system_prompt`
6. Create and initialize provider with the enriched profile (existing)
7. Set up logging (existing)

The provider receives the already-enriched `system_prompt` and handles it the same way it handles any other system prompt content. No provider needs to know about skills.

### Skill catalog injection format

The catalog block is appended to the end of the existing agent system prompt:

```markdown

## Available Skills

The following skills are available exclusively through CAO orchestration. Use the `get_skill` tool to load a skill's full content when relevant to your task. These skills are not retrievable through any other skill system or commands.

- **cao-worker-protocols**: CAO worker agent communication patterns and callback workflows
- **python-testing**: Python testing conventions using pytest, fixtures, and coverage requirements
```

The format is intentionally minimal:
- A heading to clearly delineate the skill catalog from the rest of the profile content
- A brief instruction telling the agent how to use skills
- A bulleted list of skill names (bolded) and descriptions

### Integration points

| Component | Change |
|-----------|--------|
| `models/agent_profile.py` | Add `skills: Optional[List[str]] = None` field |
| `services/terminal_service.py` | Skill resolution and prompt injection in `create_terminal` |
| `utils/skills.py` | `load_skill_metadata` (already designed) |

### Design decisions

| Decision | Rationale |
|----------|-----------|
| Injection in terminal service, not providers | Centralizes skill logic in one place. Providers remain skill-unaware and receive an already-enriched system prompt. Adding a new provider never requires implementing skill support. |
| Append catalog to system prompt | Less intrusive than prepending. The agent profile body defines the agent's identity and should appear first. The skill catalog is supplementary context. |
| No model-level skill validation | Profile loading should be fast and side-effect-free. Coupling the model to filesystem checks would make profile listing and parsing slower and harder to test. |
| Minimal catalog format | The catalog only needs to tell the agent what skills exist and how to retrieve them. Detailed content is loaded lazily via `get_skill`. |
