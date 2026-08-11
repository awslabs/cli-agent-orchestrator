# Skills

## Overview

Skills are reusable blocks of instructional content — domain knowledge, conventions, procedures, guidelines — that can be shared across agent profiles. Instead of duplicating the same instructions in every agent profile that needs them, you define the knowledge once as a skill and reference it from any profile.

Skills are loaded lazily: only the skill name and description are injected into the agent's prompt at launch. The full content is retrieved on demand when the agent decides it needs it, preserving context window budget.

The global skill store lives at `~/.aws/cli-agent-orchestrator/skills/`. There is no distinction between built-in and user-created skills — you can edit, replace, or remove any skill, including the defaults. In addition to the global store, CAO can discover skills from extra directories you register (for example a project's own skills folder) — see [Extra Skill Directories](#extra-skill-directories) below.

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

After installation, all providers pick up the new skill automatically — Copilot CLI agent files are refreshed immediately, and other providers pick up changes on the next terminal creation.

### `cao skills remove <name>`

Removes an installed skill from the skill store.

```bash
cao skills remove python-testing
```

After removal, all providers pick up the change automatically — Copilot CLI agent files are refreshed immediately, and other providers pick up changes on the next terminal creation.

### Builtin skill seeding

Builtin skills are auto-seeded when `cao-server` starts — no manual step required. If a skill with the same name already exists, it is skipped — preserving any edits you've made. After a CAO upgrade, restarting the server will seed any new builtin skills without overwriting your changes. You can also run `cao init` to seed them manually.

CAO ships with built-in skills including:

| Skill | Description |
|-------|-------------|
| `cao-agent-routing` | Finds the best installed profile for specialist work before delegation |
| `cao-supervisor-protocols` | Multi-agent orchestration patterns for supervisors: `assign`, `handoff`, idle-based message delivery |
| `cao-worker-protocols` | Worker-side callback and completion rules for assigned and handed-off tasks |

## Extra Skill Directories

Beyond the global store, CAO can discover skills from extra directories you register via the `extra_skill_dirs` setting. This mirrors `extra_agent_dirs` for agent profiles.

Unlike `cao skills add`, which **copies** a skill folder into the global store, an extra directory is **scanned in place** — nothing is copied. This lets you keep a project's skills in the project repo (e.g. `<repo>/.cao/skills`) and register that directory, so the skills stay canonical in their source location: edits are picked up on the next terminal creation, there is no second copy to keep in sync, and the skills are version-controlled alongside the project rather than re-added after every change.

Each registered directory is scanned one level deep — every immediate subfolder that contains a `SKILL.md` is treated as a skill, and subfolders without one are ignored. A registered path may therefore be a broad project root; only its skill subfolders are picked up.

**Resolution order.** Directories are searched global store first, then `extra_skill_dirs` in the configured order. The first *valid* match for a given name wins, so a skill in the global store is never shadowed by a same-named skill in a later extra directory, and an invalid (unloadable) folder does not shadow a later valid one of the same name — `cao skills list` and `load_skill` resolve a name to the same skill.

**Configuration.** Extra skill directories are stored under `skills.extra_dirs` in `~/.aws/cli-agent-orchestrator/settings.json` and managed through the `/settings/skill-dirs` API. See [configuration.md](./configuration.md#skills-skills) for the request/response format.

## Agent-Plugin-Provided Skills

A third source of skills, alongside the global store and extra directories, is
an installed **agent plugin** — a portable package conforming to the open
[Agent Plugins 1.0.0](https://agent-plugins.org/specification) specification.
See [agent-plugins.md](./agent-plugins.md) for installing and managing them.
(Not to be confused with CAO's [event plugins](./plugins.md), which react to
server-side events and provide no skills.)

```bash
cao plugin add https://github.com/owner/repo
```

**Agent-plugin skills are projected into the global store**, not scanned from a
fourth location. Each valid skill in an installed agent plugin is materialized as
a managed symlink inside `SKILLS_DIR`:

```text
~/.aws/cli-agent-orchestrator/skills/<skill-name>
    -> ~/.aws/cli-agent-orchestrator/agent-plugins/<plugin>/skills/<skill-name>
```

That is deliberate rather than incidental. Registering agent-plugin roots as
extra directories would cover the catalog-based providers and **miss Kiro CLI
and OpenCode entirely**: Kiro receives only [`skill://` globs rooted at
`SKILLS_DIR`](#kiro-cli) and OpenCode reads through one symlink to `SKILLS_DIR`,
so a skill stored anywhere else is invisible to both — including CAO's own
default provider. Projecting into the store means every mechanism described in
[How Skills Work by Provider](#how-skills-work-by-provider) picks agent-plugin
skills up with no per-provider change, and the launch path gains no new
filesystem scan.

**The link is named with the unprefixed skill name.** A skill folder's name must
equal its frontmatter `name`, so no namespacing prefix is possible without
rewriting the agent plugin's bytes — which CAO never does. Use
`cao plugin list` to see which agent plugin contributed a given skill.

**Collisions are resolved by a fixed rule, never silently:**

1. **A built-in or user-added skill always wins.** If a name is already taken by
   a seeded skill, a `cao skills add` install, or an `extra_skill_dirs` skill,
   the agent plugin's skill is not projected and a `SKIPPED` finding names the
   collision. The pre-existing skill keeps resolving exactly as before.
2. **Between two agent plugins, the lexicographically smallest manifest name
   wins**, and every loser gets a finding naming the winner. The rule reads only
   the persisted manifest name — never install order, timestamps, or directory
   scan order — so the outcome depends on *which* agent plugins are installed,
   not *how* they got there. Installing a lexicographically earlier agent plugin
   can therefore reassign an existing projection; when that happens CAO emits an
   additional warning naming both the previous and the new provider.

**Projection is derived state.** It is rebuilt from scratch on every install,
update, and removal, and swept for dangling links on `cao plugin list`. A
projected link whose target has gone is simply not enumerated — the same
`is_dir()` / `SKILL.md is_file()` gates that skip any invalid skill folder — so
it can never break a terminal launch.

On a system where symlink creation is unavailable (Windows without Developer
Mode or elevation), CAO falls back to copying the skill content and reports the
fallback. Set `"skills": {"projection_mode": "copy"}` in `settings.json` to make
that explicit.

## How Agents Discover Skills

By default, every installed skill is available to every CAO agent. When an agent is launched, CAO appends a catalog block to the prompt listing each available skill's name and description, along with instructions to use the `load_skill` MCP tool to retrieve full content. The agent then decides when and whether to load each skill based on the task at hand.

### Scoping the catalog per agent (`skills`)

To advertise only a subset of skills to a given agent, set the `skills` field in its profile frontmatter — a list of skill-name patterns, each an exact name or a case-sensitive [`fnmatch`](https://docs.python.org/3/library/fnmatch.html) glob. Only matching skills appear in that agent's catalog; the rest are simply not advertised to it. A pattern that matches no installed skill is logged as a warning, to catch typos and stale names.

This scopes the injected **catalog** only — it controls what an agent *sees* advertised, not what it can *load*. Skill resolution is unchanged: `load_skill` still resolves any installed skill by name, so this is a prompt-relevance / noise-reduction control, not an access boundary. (If you need a hard per-agent allowlist, that would have to be enforced in the `load_skill` path — out of scope here.)

This applies only to the runtime-prompt providers that receive the injected catalog (Claude Code, Codex, Antigravity CLI, Kimi CLI). Providers that deliver skills natively (Kiro CLI, OpenCode, GitHub Copilot CLI) ignore the field.

```yaml
---
name: ads-backend-developer
role: developer
skills: ["ads-db", "ads-query-logs"]   # exact names
---
```

```yaml
---
name: ads-cto
role: developer
skills: ["ads-*", "cao-*"]              # globs: this project's skills + CAO built-ins
---
```

Semantics:

- **field omitted** → the full catalog (backward-compatible default).
- **list of patterns** → only skills matching at least one pattern.
- **empty list `[]`** → no skill catalog is injected for this agent.

This keeps each agent's catalog focused — for example so one project's agents don't see another project's skills when both register their `skills/` directories under a shared `extra_skill_dirs`.

You can also explicitly instruct the agent to load specific skills eagerly in the agent profile body:

```markdown
Before starting any task, load the python-testing and code-style skills.
```

## How Skills Work by Provider

Skills are delivered to agents differently depending on the provider. The table below summarizes the mechanism for each:

| Provider | Injection Method | When Catalog Updates | Skill Retrieval |
|----------|-----------------|---------------------|-----------------|
| Claude Code | Runtime prompt | Every terminal creation | `load_skill` MCP tool |
| Codex | Runtime prompt | Every terminal creation | `load_skill` MCP tool |
| Antigravity CLI | Runtime prompt | Every terminal creation | `load_skill` MCP tool |
| Kimi CLI | Runtime prompt | Every terminal creation | `load_skill` MCP tool |
| Kiro CLI | Native `skill://` resources | Every terminal creation | Kiro progressive loading |
| Copilot CLI | Baked into `.agent.md` at install | On `cao skills add/remove` | `load_skill` MCP tool |
| OpenCode CLI | Native `skill` tool via `OPENCODE_CONFIG_DIR/skills` symlink | Every terminal creation | OpenCode progressive loading (also via `load_skill` MCP tool) |
| Cursor CLI | Runtime prompt (currently disabled — see [Cursor CLI provider docs](cursor-cli.md#agent-profile-integration)) | Not injected in v2026 | `load_skill` MCP tool |
| Hermes | Not injected — configure skills in the selected Hermes profile | N/A | N/A |

### Runtime Prompt Providers (Claude Code, Codex, Antigravity CLI, Kimi CLI)

For these providers, the skill catalog is built fresh each time a terminal is created. The catalog — a list of skill names and descriptions — is appended to the system prompt via the provider's native CLI flags.

The agent retrieves full skill content at runtime by calling the `load_skill` MCP tool, which fetches the skill body from the CAO server.

No action is needed after `cao skills add` or `cao skills remove` — the next terminal created will automatically reflect the current set of installed skills.

### Kiro CLI

Kiro has native support for `skill://` resources with progressive loading. At terminal creation, CAO includes two `skill://` glob patterns in the agent's `resources` field, both pointing at the skill store directory:

```
skill://~/.aws/cli-agent-orchestrator/skills/**/SKILL.md
skill://~/.aws/cli-agent-orchestrator/skills/*/SKILL.md
```

The second pattern is not redundant. Kiro expands these globs itself, and `**` has no single agreed meaning for directory symlinks — Python's `glob.glob(recursive=True)` descends into them while `pathlib.Path.glob` does not. [Agent-plugin](agent-plugins.md) skills are projected into the store as symlinks, so under the stricter reading they would be invisible to Kiro alone. A single-level `*/SKILL.md` has no recursive descent to opt out of, so it matches them either way, and it covers exactly the one-level-deep layout CAO guarantees. `**/SKILL.md` is kept because Kiro supports nested skill directories natively even though CAO's own catalog does not.

Kiro loads only skill metadata (name and description) at startup, then retrieves full content on demand through its own progressive loading mechanism — no MCP tool call needed.

Because Kiro reads directly from the skill store, changes from `cao skills add` or `cao skills remove` take effect the next time a terminal is created. No agent file refresh is needed.

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

Once installed, the skill is automatically available to all CAO agents. Copilot CLI agent files are refreshed immediately by the `cao skills add` command. All other providers pick up changes on the next terminal creation.

## Updating a Skill

You can edit a skill directly in the skill store:

```bash
vim ~/.aws/cli-agent-orchestrator/skills/my-coding-standards/SKILL.md
```

Or overwrite it with an updated version from a local folder:

```bash
cao skills add ./my-coding-standards --force
```

Running `cao skills add --force` refreshes Copilot CLI agent files immediately. All other providers pick up the change on the next terminal creation. If you edited the skill file directly in the store instead of using `cao skills add --force`, Copilot files won't be refreshed — run `cao skills remove <name>` followed by `cao skills add <folder>` to trigger the refresh, or reinstall the affected agents with `cao install`.

## Known Limitations

- **No nested skill directories.** Skills must be immediate subdirectories of the skill store. Nested paths (e.g., `skills/team/python-testing/`) are not discovered by CAO's skill catalog. Kiro's `skill://` glob handles nested paths natively, but other providers do not.
- **Extra-directory skills do not reach Kiro CLI or OpenCode CLI.** These two providers discover skills by traversing the skill store path directly — Kiro through the `skill://` resource globs baked into its agent JSON, OpenCode through the `OPENCODE_CONFIG_DIR/skills` symlink that points at the skill store. Both are rooted at the store, so skills registered via [extra skill directories](#extra-skill-directories), which live outside it, are invisible to them. The catalog-based providers (Claude Code, Codex, Antigravity CLI, Kimi CLI, Copilot CLI) are unaffected, because they receive a catalog built by CAO's own scan, which searches the store *and* every registered extra directory. To make a skill reachable by every provider, install it into the global store (`cao skills add <folder>`) rather than registering its parent directory. The provider table above describes the injection mechanism, not extra-directory coverage.
- **Catalog scoping is advertise-only.** The per-agent [`skills`](#scoping-the-catalog-per-agent-skills) field filters which skills appear in an agent's injected catalog, but it does not restrict resolution — `load_skill` still resolves any installed skill by name. It is a prompt-relevance control, not an access boundary; a hard per-agent allowlist would need enforcement in the `load_skill` path.
