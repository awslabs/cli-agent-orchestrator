# Skills

## Overview

Skills are reusable blocks of instructional content — domain knowledge, conventions, procedures, guidelines — that can be shared across agent profiles. Instead of duplicating the same instructions in every agent profile that needs them, you define the knowledge once as a skill and reference it from any profile.

Skills are loaded lazily: only the skill name and description are injected into the agent's prompt at launch. The full content is retrieved on demand when the agent decides it needs it, preserving context window budget.

All skills live in a single directory: `~/.aws/cli-agent-orchestrator/skills/`. There is no distinction between built-in and user-created skills — you can edit, replace, or remove any skill, including the defaults.

## When to Use Skills

Use skills when:

- **Multiple agents need the same knowledge.** Testing conventions, coding standards, deployment procedures, or communication protocols that apply across agent profiles.
- **You want to keep agent profiles focused.** Profiles should define *who* the agent is (role, tools, MCP servers). Skills define *what* the agent knows how to do.
- **You want to save context window budget.** An agent working on a simple file rename doesn't need a 2,000-word database migration guide loaded upfront. With skills, the agent loads the full content only when it's relevant.
- **You need organization-specific knowledge.** Custom skills for your team's internal tooling, review processes, or domain-specific workflows.

## Skill File Structure

A skill is a folder containing a `SKILL.md` file. The folder name must match the `name` field in the YAML frontmatter.

```
python-testing/
└── SKILL.md
```

`SKILL.md` has two required frontmatter fields — `name` and `description` — followed by the skill content in Markdown:

```markdown
---
name: python-testing
description: Python testing conventions using pytest, fixtures, and coverage requirements
---

# Python Testing Conventions

Use pytest for all test files. Place tests in a `test/` directory mirroring
the `src/` structure...
```

The `description` is what the agent sees at launch to decide whether to load the skill. Write it to be informative enough for the agent to make that judgment.

## CLI Commands

### `cao skills list`

Lists all installed skills with their name and description.

```
$ cao skills list
Name                        Description
cao-supervisor-protocols    Supervisor-side orchestration patterns for assign, handoff, and idle inbox delivery in CAO
cao-worker-protocols        Worker-side callback and completion rules for assigned and handed-off tasks in CAO
```

### `cao skills add <folder-path> [--force]`

Installs a skill from a local folder into the skill store.

```bash
# Install a new skill
cao skills add ./python-testing

# Overwrite an existing skill
cao skills add ./python-testing --force
```

Validation checks (in order):
1. Path is a directory
2. Directory contains a `SKILL.md` file
3. Frontmatter has non-empty `name` and `description`
4. Folder name matches the frontmatter `name`
5. No path traversal characters in the name (`/`, `\`, `..`)
6. Skill does not already exist (unless `--force` is passed)

After installation, all CAO-managed Q CLI and Copilot CLI agent files are automatically refreshed to include the new skill in their prompt catalog.

### `cao skills remove <name>`

Removes an installed skill from the skill store.

```bash
cao skills remove python-testing
```

After removal, all CAO-managed Q CLI and Copilot CLI agent files are automatically refreshed to remove the skill from their prompt catalog.

### `cao init` (skill seeding)

Running `cao init` seeds default skills into the skill store. If a skill with the same name already exists, it is skipped — preserving any edits you've made. Re-running `cao init` after a CAO upgrade will seed any new default skills without overwriting your changes.

CAO ships with two default skills:

| Skill | Description |
|-------|-------------|
| `cao-supervisor-protocols` | Multi-agent orchestration patterns for supervisors: `assign`, `handoff`, idle-based message delivery |
| `cao-worker-protocols` | Worker-side callback and completion rules for assigned and handed-off tasks |

## How Agents Discover Skills

All installed skills are available to all CAO agents — there is no per-profile skill declaration. When an agent is launched, CAO appends a catalog block to the prompt listing every installed skill's name and description, along with instructions to use the `load_skill` MCP tool to retrieve full content. The agent then decides when and whether to load each skill based on the task at hand.

You can explicitly instruct the agent to load specific skills eagerly in the agent profile body:

```markdown
Before starting any task, load the python-testing and code-style skills.
```

## How Skills Work by Provider

Skills are delivered to agents differently depending on the provider. The table below summarizes the mechanism for each:

| Provider | Injection Method | When Catalog Updates | Skill Retrieval |
|----------|-----------------|---------------------|-----------------|
| Claude Code | Runtime prompt | Every terminal creation | `load_skill` MCP tool |
| Codex | Runtime prompt | Every terminal creation | `load_skill` MCP tool |
| Gemini CLI | Runtime prompt | Every terminal creation | `load_skill` MCP tool |
| Kimi CLI | Runtime prompt | Every terminal creation | `load_skill` MCP tool |
| Kiro CLI | Native `skill://` resources | Every terminal creation | Kiro progressive loading |
| Q CLI | Baked into agent JSON at install | On `cao skills add/remove` | `load_skill` MCP tool |
| Copilot CLI | Baked into `.agent.md` at install | On `cao skills add/remove` | `load_skill` MCP tool |

### Runtime Prompt Providers (Claude Code, Codex, Gemini CLI, Kimi CLI)

For these providers, the skill catalog is built fresh each time a terminal is created. The catalog — a list of skill names and descriptions — is appended to the system prompt via the provider's native CLI flags.

The agent retrieves full skill content at runtime by calling the `load_skill` MCP tool, which fetches the skill body from the CAO server.

No action is needed after `cao skills add` or `cao skills remove` — the next terminal created will automatically reflect the current set of installed skills.

### Kiro CLI

Kiro has native support for `skill://` resources with progressive loading. At terminal creation, CAO includes a `skill://` glob pattern in the agent's `resources` field that points to the skill store directory:

```
skill://~/.aws/cli-agent-orchestrator/skills/**/SKILL.md
```

Kiro loads only skill metadata (name and description) at startup, then retrieves full content on demand through its own progressive loading mechanism — no MCP tool call needed.

Because Kiro reads directly from the skill store, changes from `cao skills add` or `cao skills remove` take effect the next time a terminal is created. No agent file refresh is needed.

### Q CLI

The skill catalog is baked into the agent's JSON file (`~/.q/agents/{name}.json`) at install time via `cao install`. The `prompt` field in the JSON contains the agent's prompt with the skill catalog appended.

When you run `cao skills add` or `cao skills remove`, all CAO-managed Q agent files are automatically refreshed — their `prompt` field is rewritten with the updated skill catalog.

CAO identifies Q agents it manages by checking whether the agent's `resources` field contains a `file://` URI pointing to the CAO agent context directory (`~/.aws/cli-agent-orchestrator/agent-context/`).

### Copilot CLI

The skill catalog is baked into the agent's `.agent.md` file (`~/.copilot/agents/{name}.agent.md`) at install time. The Markdown body of the file contains the agent's prompt with the skill catalog appended. The YAML frontmatter (`name`, `description`) is preserved during refreshes.

When you run `cao skills add` or `cao skills remove`, all CAO-managed Copilot agent files are automatically refreshed — their body content is rewritten with the updated skill catalog while preserving frontmatter.

CAO identifies Copilot agents it manages by checking whether a matching agent context file exists in `~/.aws/cli-agent-orchestrator/agent-context/`.

## Creating a Custom Skill

1. Create a folder with your skill name:

```bash
mkdir my-coding-standards
```

2. Create a `SKILL.md` file inside it:

```markdown
---
name: my-coding-standards
description: Team coding standards for Python services including naming, error handling, and logging
---

# Coding Standards

## Naming Conventions

- Use snake_case for functions and variables
- Use PascalCase for classes
...
```

3. Install the skill:

```bash
cao skills add ./my-coding-standards
```

Once installed, the skill is automatically available to all CAO agents. Runtime prompt providers (Claude Code, Codex, Gemini CLI, Kimi CLI) and Kiro will pick it up on the next terminal creation. Q CLI and Copilot CLI agent files are refreshed automatically by the `cao skills add` command.

## Updating a Skill

You can edit a skill directly in the skill store:

```bash
vim ~/.aws/cli-agent-orchestrator/skills/my-coding-standards/SKILL.md
```

Or overwrite it with an updated version from a local folder:

```bash
cao skills add ./my-coding-standards --force
```

For runtime prompt providers (Claude Code, Codex, Gemini CLI, Kimi CLI) and Kiro, changes take effect on the next terminal creation. For Q CLI and Copilot CLI, running `cao skills add --force` automatically refreshes all installed agent files. If you edited the skill file directly instead, run `cao skills remove <name>` followed by `cao skills add <folder>` to trigger the refresh — or reinstall the affected agents with `cao install`.

## Known Limitations

- **No nested skill directories.** Skills must be immediate subdirectories of the skill store. Nested paths (e.g., `skills/team/python-testing/`) are not discovered by CAO's skill catalog. Kiro's `skill://` glob handles nested paths natively, but other providers do not.
- **No per-profile skill scoping.** All installed skills are available to all agents. There is currently no way to restrict which skills a specific agent profile can see. A `skills` field in agent profile frontmatter for declaring allowed skills is a planned future addition.
