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

A skill is a Markdown file with YAML frontmatter containing a `name` and `description`. The body contains the full skill content. Skills are stored in two locations:

- **Built-in:** `src/cli_agent_orchestrator/skills/` (shipped with CAO)
- **User-installed:** `~/.aws/cli-agent-orchestrator/skills/`

### Agent profile integration

Agent profiles gain a `skills` field in their YAML frontmatter — a list of skill names that the agent has access to.

### Server-side metadata cache

On server startup (and on skill install), the CAO server reads and caches skill metadata (name and description) from both the built-in and user-installed skill directories. This cache enables efficient prompt injection at terminal creation time without reading skill files on every launch.

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
