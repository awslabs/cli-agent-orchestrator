# Design: OpenCode CLI Provider

## 1. Overview

This document captures the design for integrating [OpenCode](https://opencode.ai) as a CLI
agent provider in CAO. OpenCode is a terminal-based AI assistant with a native agent system
whose file format (Markdown with YAML frontmatter) is nearly identical to CAO's own agent
profile format, making it the cleanest provider integration to date after Kiro CLI.

The integration follows the Kiro provider pattern: CAO translates its agent profiles into
OpenCode's native agent format at install time and launches terminals with OpenCode's
`--agent <name>` flag, rather than composing single-shot prompts. This leverages OpenCode's
native tool/permission/model selection rather than reimplementing those concerns in CAO.

Status: **design ready for implementation**, pending the remaining low-severity items in
[§10 Risks and Open Questions](#10-risks-and-open-questions). TUI output patterns
([§10.1](#101-tui-output-patterns--resolved)), first-run workspace trust
([§10.2](#102-first-run-workspace-trust-prompt--resolved)), and exit command
([§10.5](#105-exit-command--resolved)) were verified empirically via tmux probe on
2026-04-20.

## 2. Scope

### In Scope

- New `opencode_cli` provider class with standard `initialize / get_status /
  extract_last_message_from_script / exit_cli / cleanup` lifecycle
- `cao install --provider opencode_cli` branch that emits OpenCode-native agent files and
  merges MCP/tool-gating config into a CAO-owned `opencode.json`
- Config isolation from the user's personal OpenCode workflow via `OPENCODE_CONFIG` and
  `OPENCODE_CONFIG_DIR` env vars pointing at `~/.aws/opencode/`
- Per-agent MCP server gating via OpenCode's `agent.<name>.tools` config section
- Auto-approve support by emitting `permission:` frontmatter at install time when CAO is
  launched with `--auto-approve`
- Unit tests, e2e tests, and provider docs (per the `cao-provider` skill checklist)

### Out of Scope

- **`opencode run` (single-shot) integration** — CAO needs a persistent REPL; TUI mode is
  the only fit. Future single-shot use cases could layer on top.
- **`opencode serve` / `opencode attach` / `opencode acp`** — alternative transports that do
  not fit CAO's tmux-centric architecture. Documented as possible future adapters if TUI
  parsing proves too fragile.
- **Session resumption across CAO restarts** (`--continue` / `--session`) — CAO's model is
  fresh-terminal-per-agent; session persistence is not needed today.
- **Handling project-local `opencode.json` collisions** — out of scope for CAO to manage
  the user's project directory state (see [§10](#10-risks-and-open-questions)).

## 3. Architectural Alignment

| CAO requirement | OpenCode mechanism | Fit |
|---|---|---|
| REPL-style persistent terminal | `opencode [project]` (TUI default mode)[^1] | Native |
| Agent selection at launch | `--agent <name>` flag on TUI and `run`[^1] | Native |
| Agent profile on disk | `<OPENCODE_CONFIG_DIR>/agents/<name>.md` (Markdown + YAML frontmatter)[^2] | Near-identical to CAO's own format |
| Model override | `--model provider/model` CLI flag at launch[^1] | **Exception to native-first** — see [§3.1](#31-integration-philosophy-native-first) |
| Tool restrictions | `permission:` frontmatter field (`allow`/`ask`/`deny` per tool)[^2][^4] | Native — no CAO `TOOL_MAPPING` entry needed |
| MCP servers | Global `opencode.json` under `mcp`; per-agent gating via `agent.<name>.tools`[^3] | Native but requires config merging (see [§6](#6-mcp-wiring-and-per-agent-gating)) |
| Auto-approve permissions | `permission: allow` in agent frontmatter[^2][^4] | Native via frontmatter — per [§3.1](#31-integration-philosophy-native-first) (see [§7](#7-permissions-and-auto-approve)) |
| Isolation from user's workflow | `OPENCODE_CONFIG` + `OPENCODE_CONFIG_DIR` env vars[^5] | Native — see [§5](#5-config-isolation) |

### 3.1 Integration Philosophy: native-first

When mapping a CAO capability onto OpenCode, prefer mechanisms in this order:

1. **Agent frontmatter field** (e.g., `permission:`, `model:`, `mode:`) — closest to the
   CAO profile mental model, declarative, survives restart, colocated with the rest of the
   agent's config.
2. **Shared config file** (`opencode.json`) — for things that are genuinely cross-agent or
   not expressible in frontmatter (e.g., `mcp` server declarations, per-agent `tools`
   gating).
3. **CLI flags** (`--agent`, `--model`) — for per-launch selection that isn't part of
   persistent agent state.
4. **Environment variables** (`OPENCODE_CONFIG_DIR`, `OPENCODE_CONFIG`, etc.) — reserved
   for things OpenCode only exposes via env.

This ordering matters because it minimizes the state CAO has to maintain separately and
keeps the mental model consistent: each CAO agent's behavior is fully described by its
installed `.md` file, with `opencode.json` holding only genuinely shared concerns. The
exceptions in tier 4 are documented on a case-by-case basis.

**Documented exception: model selection.** `profile.model` is passed via the `--model`
CLI flag at `cao launch` time, not written to frontmatter. This aligns with the existing
CAO pattern from fix #189 ("honor profile.model at terminal creation") and lets a model
be overridden per-launch without reinstalling the agent — a useful knob given the common
cost/speed/quality tradeoffs when switching models across different tasks. No other field
currently warrants this exception.

## 4. Agent Profile Translation

`cao install --provider opencode_cli` writes two artifacts, analogous to the existing Kiro
branch at `src/cli_agent_orchestrator/cli/commands/install.py:195`:

1. `~/.aws/opencode/agents/<profile.name>.md` — OpenCode agent file
2. Upserts into `~/.aws/opencode/opencode.json` — MCP + tool gating (see [§6](#6-mcp-wiring-and-per-agent-gating))

### Frontmatter field mapping

| CAO profile field | OpenCode frontmatter field | Notes |
|---|---|---|
| `name` | (filename `<name>.md`)[^2] | Filename is the agent ID |
| `description` | `description` | 1:1 |
| `model` | _(not in frontmatter)_ | Passed via `--model` at launch time — see [§3.1](#31-integration-philosophy-native-first) documented exception |
| `allowedTools` | `permission: {<tool>: allow\|deny}` | CAO vocabulary → OpenCode tool names (see [§9](#9-tool-vocabulary)) |
| `mcpServers` | _(not in frontmatter — see [§6](#6-mcp-wiring-and-per-agent-gating))_ | Goes into `opencode.json` |
| `prompt` / `system_prompt` | _(Markdown body of the file)_ | OpenCode reads body as system prompt[^2] |
| _(implicit)_ | `mode: all` | Ensures agent is selectable via `--agent` and as a subagent |

The Markdown body contains only `profile.system_prompt` (or `profile.prompt` as fallback).
Unlike the Q/Copilot install branches, CAO does **not** bake the skill catalog into the
body for OpenCode — skills are delivered natively via OpenCode's `skills/` discovery
directory (see [§5.1](#skill-delivery-native-discovery)). This mirrors how the Kiro
provider uses `skill://` resources for progressive loading, and leaves the agent's system
prompt lean.

### New code

- `src/cli_agent_orchestrator/models/opencode_agent.py` — `OpenCodeAgentConfig` Pydantic
  model (analogous to `kiro_agent.py`, `q_agent.py`)
- `src/cli_agent_orchestrator/utils/opencode_permissions.py` — translator:
  CAO `allowedTools` list → OpenCode `permission:` dict
- `src/cli_agent_orchestrator/utils/opencode_config.py` — atomic read-modify-write helper
  for the shared `opencode.json`

Install uses `frontmatter.dumps()` the same way the Copilot branch does at `install.py:246`.

## 5. Config Isolation

OpenCode exposes two relevant env vars[^5]:

| Env var | Type | Effect |
|---|---|---|
| `OPENCODE_CONFIG` | file path | Location of a merged-in `opencode.json` |
| `OPENCODE_CONFIG_DIR` | directory | Root searched for `agents/`, `commands/`, `modes/`, `plugins/` subdirectories |

CAO sets **both** at launch time so that CAO-managed agents and MCP wiring never collide
with the user's personal OpenCode setup under `~/.config/opencode/`. Verified 2026-04-20:
setting both env vars to point at CAO-owned locations isolates agent discovery and config
while still merging cleanly with OpenCode's built-in defaults (`build`, `plan`, `explore`,
etc. remain available; CAO-installed agents appear in `opencode agent list` alongside
them).

### Storage layout

```
~/.aws/opencode/
├── opencode.json          # MCP servers + per-agent tool gating
├── package.json           # written by opencode on first launch
├── package-lock.json      # written by opencode on first launch
├── node_modules/          # ~57MB, installed by opencode on first launch
├── agents/
│   ├── code_supervisor.md
│   ├── developer.md
│   └── ...
└── skills/                # symlink → ~/.aws/cli-agent-orchestrator/skills/
```

**First-launch install side-effect.** OpenCode treats `OPENCODE_CONFIG_DIR` as a mutable
install location, not just a read-only config dir. On first launch it creates
`package.json` declaring a pinned dependency on `@opencode-ai/plugin` (version-matched to
the `opencode` binary) and runs `npm install` — ~57MB of dependencies. This blocks the TUI
from painting for 5–30 seconds on cold start. Handled naturally by the `wait_until_status`
polling loop with a generous timeout (see [§8.2](#82-initialize)); no explicit pre-warm
step is needed.

### 5.1 Skill delivery (native discovery)

CAO's skill store at `SKILLS_DIR` (`~/.aws/cli-agent-orchestrator/skills/`, see
`constants.py:79`) already follows OpenCode's `skills/<name>/SKILL.md` convention exactly —
same directory layout, same YAML frontmatter (`name`, `description`), and CAO's existing
skill names (`cao-supervisor-protocols`, `cao-worker-protocols`) match OpenCode's required
regex `^[a-z0-9]+(-[a-z0-9]+)*$`[^sk].

**Approach:** at `cao install --provider opencode_cli` time, ensure a symlink at
`OPENCODE_CONFIG_DIR/skills` pointing at `SKILLS_DIR`. OpenCode auto-discovers
`<OPENCODE_CONFIG_DIR>/skills/` and exposes contents through its native `skill` tool with
progressive loading (metadata listed up front, full bodies loaded on demand)[^sk].

**Consequences:**

- The OpenCode install branch writes only `profile.system_prompt` / `profile.prompt` as
  the agent body. It does **not** call `compose_agent_prompt(profile)` — the skill catalog
  stays out of the system prompt entirely (see [§4](#4-agent-profile-translation)).
- Skill additions / removals under `SKILLS_DIR` are live for OpenCode agents on the next
  launch without any `refresh_installed_agent_*` pass. The symlink is a directory
  reference, not a snapshot.
- CAO's `load_skill` MCP tool remains exposed for cross-provider parity. OpenCode agents
  thus see two loaders for the same content (native `skill` tool + MCP `load_skill`);
  redundant but harmless, and preserves a single orchestration-level contract.
- `SKILL_CATALOG_INSTRUCTION` in `utils/skills.py:15-19` (the prose telling agents that
  skills "are not accessible through provider-native skill commands or directories") is
  not injected into OpenCode agents — the statement would be false for this provider.
- **Platform caveat:** symlinks are created on Linux and macOS (CAO's declared targets).
  Windows is outside current scope; revisit if a Windows target is added.

The ensure-symlink helper is idempotent and unguarded: if the target path already exists
as something other than the expected symlink (e.g., a real directory a user created
manually), we do not try to detect or repair it. Deferred until a real user report.

### Constants (new)

In `src/cli_agent_orchestrator/constants.py`:

```python
OPENCODE_CONFIG_DIR = Path.home() / ".aws" / "opencode_cli"
OPENCODE_AGENTS_DIR = OPENCODE_CONFIG_DIR / "agents"
OPENCODE_CONFIG_FILE = OPENCODE_CONFIG_DIR / "opencode.json"
```

### Launch command shape

Env vars are passed inline on the same `send_keys` invocation — avoids a second shell round
trip and scopes the vars to the `opencode` process only:

```bash
OPENCODE_CONFIG=~/.aws/opencode/opencode.json \
OPENCODE_CONFIG_DIR=~/.aws/opencode \
OPENCODE_DISABLE_AUTOUPDATE=1 \
OPENCODE_DISABLE_MOUSE=1 \
OPENCODE_DISABLE_TERMINAL_TITLE=1 \
OPENCODE_CLIENT=cao \
TERM=xterm-256color \
opencode --agent <name> [--model <provider/model>]
```

`--model` is appended only when `profile.model` is set (see
[§3.1](#31-integration-philosophy-native-first) documented exception). Constructed via
`shlex.join(...)`, same pattern as `src/cli_agent_orchestrator/providers/kiro_cli.py:161`.

### Stability env vars

- `OPENCODE_DISABLE_AUTOUPDATE=1` — prevent version-check pause on startup[^1]
- `OPENCODE_DISABLE_MOUSE=1` — when mouse reporting is enabled, OpenCode claims scroll events and the user (or any tmux automation) can scroll the TUI's conversation history. The footer (`ctrl+p commands`, `esc interrupt`) is pinned and remains visible regardless of scroll position, so IDLE and PROCESSING detection are unaffected. However, the completion marker (`▣ <agent> · <model> · Ns`) is conversation content — not a fixed footer — and scrolling up moves it off the captured frame. Since COMPLETED detection requires both the completion marker and the idle footer to be present simultaneously, scrolling during an active turn would prevent COMPLETED from ever being detected even after the agent finishes. Disabling mouse removes this risk by keeping the frame locked to the most recent render. Side effect: tmux falls back to copy-mode on mouse-wheel scroll (the pane never claimed mouse events, so tmux intercepts them instead).[^1]
- `OPENCODE_DISABLE_TERMINAL_TITLE=1` — don't clobber tmux window titles[^1]
- `OPENCODE_CLIENT=cao` — clean telemetry/identification[^1]
- `TERM=xterm-256color` — per `cao-provider` skill lesson #15 (Kiro hit issues with unusual TERM values)

## 6. MCP Wiring and Per-Agent Gating

OpenCode configures MCP servers globally under the `mcp` key in `opencode.json`, with
per-agent gating via the `agent.<name>.tools` section[^3]. MCP tool names follow the pattern
`<servername>_*` (confirmed: `"mymcpservername_*": false` disables all tools for that server[^3]).

### The pattern

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "cao-mcp-server": {
      "type": "local",
      "command": ["cao-mcp-server"],
      "enabled": true
    }
  },
  "tools": {
    "cao-mcp-server*": false
  },
  "agent": {
    "code_supervisor": {
      "tools": { "cao-mcp-server*": true }
    },
    "developer": {
      "tools": { "cao-mcp-server*": true }
    }
  }
}
```

Default-deny at the top level plus explicit per-agent re-enable gives belt-and-braces
safety even if a project-local `opencode.json` merges on top (see [§10](#10-risks-and-open-questions)).

### Per-terminal `CAO_TERMINAL_ID` injection

`CAO_TERMINAL_ID` varies per terminal but `opencode.json` is a single static file. Solution:
OpenCode spawns the MCP subprocess which inherits the tmux window's environment, so
`CAO_TERMINAL_ID` set on the tmux window at launch time propagates to the MCP server
naturally. The `mcp.<name>.environment` field in `opencode.json` is left empty and relies
on parent-env inheritance — same pattern as how Kiro already works.

### Install-time upserts into `opencode.json`

For each `cao install --provider opencode_cli`, idempotent edits:

1. Add each `profile.mcpServers` entry under top-level `mcp` if not already present
2. Add `"<servername>*": false` under top-level `tools` (default-deny)
3. Set `agent.<profile.name>.tools = {"<servername>*": true, ...}` for the MCP servers this
   agent declares

**MCP name-collision policy: not handled.** If two CAO agents declare an `mcpServer` with
the same name but different `command`/`environment`, the second install silently overwrites
the first under top-level `mcp`. Users are assumed to keep MCP names globally consistent
across their CAO agent profiles. This matches how other providers (Kiro, Q) implicitly
treat MCP names as globally unique.

**Concurrent-write policy: not handled in v1.** Unlike Kiro/Q/Copilot (which each write one
file per agent), OpenCode has a single shared `opencode.json` that every `cao install`
touches. If two `cao install --provider opencode_cli` commands run in parallel (e.g., via a
batch script, or via a future `cao-ops-mcp install_profile` tool called concurrently),
they can race on the read-modify-write cycle and the second writer may clobber the first's
agent entry. Sequential installs are safe. File locking is deferred until the case is
actually observed — the targeted fix is ~5 lines of `fcntl.flock` around the editor.

Uninstall (future) would remove only `agent.<profile.name>` without disturbing other agents'
entries or the global `mcp` map.

## 7. Permissions and Auto-Approve

OpenCode's permission model supports `allow` / `ask` / `deny` per tool, with granular
per-command patterns for tools like `bash`[^4]. `--dangerously-skip-permissions` is a flag
on `opencode run` only, not the TUI[^1], so CAO cannot use a CLI flag to auto-approve
TUI-launched agents.

Per the native-first principle ([§3.1](#31-integration-philosophy-native-first)), the
strategy is simply:

**Write permissions into agent frontmatter at install time.**

When `--auto-approve` is set on `cao launch`, `cao install` emits a `permission:` block
declaring `allow` for the tools the agent is permitted to use. When `--auto-approve` is
not set, CAO emits `ask` (or the CAO-level default) and lets OpenCode prompt the user
through its native `△ Permission required` UI (see [§8.3](#83-waiting_user_answer-detection)).

Example frontmatter emitted for an auto-approved developer agent:

```yaml
---
description: Developer agent
mode: all
model: anthropic/claude-sonnet-4-6
permission:
  edit: allow
  write: allow
  read: allow
  bash: allow
  grep: allow
  glob: allow
---
```

**Rejected alternative:** duplicating permissions into `agent.<name>.permission` inside
`opencode.json` — violates native-first; frontmatter is the higher-tier location and
duplicating data invites drift.

## 8. Runtime (Provider Class)

New file: `src/cli_agent_orchestrator/providers/opencode_cli.py`, modeled directly on
`src/cli_agent_orchestrator/providers/kiro_cli.py`. OpenCode is a 24-bit-truecolor
alt-screen TUI (verified empirically — see §10.1), so the overall architecture mirrors
the Kiro TUI branch.

### 8.1 Regex patterns (verified from probe fixtures)

Defined at module scope. All matching is done on ANSI-stripped output using the same
pattern Kiro uses (`r"\x1b\[[0-9;]*m"`, which handles 24-bit `\x1b[38;2;R;G;Bm` sequences
correctly).

```python
# User message indent — blue vertical bar + 2 spaces
USER_MESSAGE_PATTERN = r"^┃\s{2}"

# Per-turn completion marker: "▣  <agent>  ·  <model>  ·  <duration>s"
# Analog of Kiro's "▸ Credits:" line. Emitted once per agent turn.
COMPLETION_MARKER_PATTERN = r"▣\s+\S+\s+·\s+.+?\s+·\s+\d+(?:\.\d+)?s"

# Processing footer — keybind hint only shown while agent is working.
# Accompanied by a [■⬝]+ progress spinner, but the text is the stable anchor.
PROCESSING_FOOTER_PATTERN = r"\besc interrupt\b"

# Idle footer anchor — keybind hints shown when waiting for input.
IDLE_FOOTER_PATTERN = r"ctrl\+p\s+commands"

# Permission prompt heading. Two variants captured: the initial request and the
# "Always allow" sub-confirmation.
PERMISSION_PROMPT_PATTERN = r"△\s+(?:Permission required|Always allow)\b"

# Tool-call in-flight spinner (braille): "⠋ Read <path>" etc. Resolves to "→" on completion.
TOOL_CALL_IN_FLIGHT_PATTERN = r"^\s+[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s+\S+"
```

### 8.2 `initialize()`

1. `wait_for_shell()` in the tmux window
2. `send_keys()` the inline-env `opencode --agent <name>` command (see [§5](#5-config-isolation))
3. `wait_until_status({IDLE, COMPLETED}, timeout=120.0)` — matching the precedent in
   `src/cli_agent_orchestrator/providers/kimi_cli.py:349-354` ("Longer timeout (120s) to
   account for first-run setup and when multiple instances are starting concurrently").
   This single mechanism covers:
   - Steady-state launches (~2s to IDLE)
   - First-ever launch with `node_modules` install (5–30s, see [§5](#5-config-isolation))
   - Launch after an `opencode` binary upgrade that invalidates the pinned plugin version
     (re-triggers install, same 5–30s)
   - `OPENCODE_CONFIG_DIR` wipe/corruption (self-healing: install runs again)

   During the install window, `get_status()` sees shell output with no opencode markers
   and returns `ERROR`. That is not a terminal-failure state for the polling loop in
   `src/cli_agent_orchestrator/utils/terminal.py:72-78` — it just means "keep polling."
   Once the TUI paints the idle splash frame, `IDLE` matches and `initialize()` returns.

No first-run workspace-trust handling is needed — verified absent (see [§10.2](#102-first-run-workspace-trust-prompt-resolved)).

### 8.3 `get_status()` — priority order

Matches the `cao-provider` skill's recommended order. Position-aware per lesson #16 (stale
alt-screen redraws):

1. **Strip ANSI codes** from captured output.
2. **WAITING_USER_ANSWER** — `PERMISSION_PROMPT_PATTERN` present and no `IDLE_FOOTER_PATTERN`
   match appears after its position. (The permission UI replaces the normal footer with
   `enter confirm` keybinds.)
3. **PROCESSING** — `PROCESSING_FOOTER_PATTERN` matches AND no `COMPLETION_MARKER_PATTERN`
   or `IDLE_FOOTER_PATTERN` match appears after its position. The position guard handles
   the alt-screen case where `esc interrupt` text lingers briefly after the turn ends.
4. **COMPLETED** — `COMPLETION_MARKER_PATTERN` matches AND `IDLE_FOOTER_PATTERN` appears
   after the last completion marker's position. (Both must be visible together in the
   current frame.)
5. **IDLE** — `IDLE_FOOTER_PATTERN` matches AND no `PROCESSING_FOOTER_PATTERN` anywhere.
   Covers both the centered-splash first-launch frame and the post-completion input-ready
   frame.
6. **ERROR** — fallback when nothing above matches and the output is non-empty.

### 8.4 `extract_last_message_from_script()`

The agent turn is bracketed by:
- **Start**: last `USER_MESSAGE_PATTERN` match (user's most recent message). The actual
  response begins after the next non-`┃` block (user messages use `┃  ` indent; agent
  responses use 5-space indent without the bar).
- **End**: first `COMPLETION_MARKER_PATTERN` match at or after that position.

Strategy:

1. Strip ANSI once.
2. Find all `USER_MESSAGE_PATTERN` positions — take the last one (last user turn).
3. Find the first `COMPLETION_MARKER_PATTERN` match after that position.
4. Extract text between the end of the user-message block and the start of the completion
   marker.
5. Strip the `Thinking: ...` preamble if present (it's an inline assistant-thinking block
   that appears before the final response text).
6. Dedent the 5-space agent indent.
7. Clean up control chars and trailing whitespace.

Edge cases:
- **Tool calls mid-turn** — lines like `→ Read /etc/hostname` that appear between thinking
  and response. These are part of the turn; keep or strip based on whether CAO wants raw
  vs final-only output. Default: include them (downstream can post-process).
- **Multi-turn thinking+response** — some models interleave `Thinking:` and response
  blocks. Treat everything between user message and completion marker as the response.

### 8.5 `exit_cli()`

Returns `"/exit"` — matches the Kiro provider idiom
(`src/cli_agent_orchestrator/providers/kiro_cli.py:468-470`). `Ctrl+C` also exits cleanly
and is available as a fallback in `cleanup()` if typed `/exit` ever fails to dismiss the
TUI.

### 8.6 Workspace access

Add `"opencode_cli"` to `PROVIDERS_REQUIRING_WORKSPACE_ACCESS` in
`src/cli_agent_orchestrator/cli/commands/launch.py`.

## 9. Tool Vocabulary

OpenCode's full built-in tool list (confirmed empirically 2026-04-20 via an in-TUI
self-inspection):

| Tool | Purpose |
|---|---|
| `read` | Read files from filesystem |
| `write` | Write files to filesystem |
| `edit` | Edit/rewrite files |
| `glob` | Fast file pattern matching |
| `grep` | Search file contents with regex |
| `bash` | Execute bash commands |
| `task` | Launch sub-agents |
| `question` | Ask user clarifying questions |
| `webfetch` | Fetch web content |
| `websearch` | Search the web |
| `codesearch` | Search programming examples |
| `skill` | Load specialized skills |
| `todowrite` | Track task lists |

Plus per-MCP-server tools named `<servername>_*`[^3].

Tool enforcement happens inside OpenCode at runtime based on the `permission:` frontmatter
field, so **no entry in `src/cli_agent_orchestrator/utils/tool_mapping.py` is needed** —
this matches the Kiro/Q pattern (native enforcement via agent config).

CAO → OpenCode permission translation, implemented in `utils/opencode_permissions.py`.
The default posture is `deny` — any tool not explicitly allowed by the agent's
`allowedTools` is denied. Translation proceeds in two steps:

**Step 1: Expansion of CAO shorthand.** Applied to the input `allowedTools` list before
per-tool mapping:

| Input | Expands to |
|---|---|
| `*` | Unrestricted: every OpenCode tool (including the non-CAO-vocabulary ones below) set to `allow` — bypasses the default-deny policy entirely |
| `@builtin` | `execute_bash`, `fs_read`, `fs_write`, `fs_list` — the standard builtin suite |
| `@<mcp-server-name>` | Handled via `opencode.json` `agent.<name>.tools` — not in frontmatter. See [§6](#6-mcp-wiring-and-per-agent-gating) |

**Step 2: CAO category → OpenCode tool mapping.** For each expanded category in
`allowedTools`, set the listed OpenCode tools to `allow` (or `ask` when `--auto-approve`
is not set). Tools not listed stay at the default-deny.

| CAO category | OpenCode tools to `allow` |
|---|---|
| `execute_bash` | `bash` |
| `fs_read` | `read` |
| `fs_write` | `edit`, `write` |
| `fs_list` | `glob`, `grep` |
| `fs_*` | `read`, `edit`, `write`, `glob`, `grep` |

**Non-CAO-vocabulary OpenCode tools** have hardcoded policies independent of
`allowedTools` (the only way to override these is via `*`, which unrestricts everything):

| Tool | Policy | Rationale |
|---|---|---|
| `task` | `deny` | OpenCode's sub-agent launch conflicts with CAO's orchestration model — a sub-agent spawned via `task` lives inside the same opencode session, escapes CAO's terminal tracking, and isn't visible in the session list. CAO's `handoff`/`assign`/`send_message` primitives are the supported delegation path. |
| `question` | `deny` | Interactive prompt to a human user. CAO agents running under `assign`/`handoff` have no human at the terminal to answer, and the flow would stall indefinitely if the agent invoked this. |
| `todowrite` | `allow` | In-memory task list; no filesystem side-effect. Harmless. |
| `skill` | `allow` | Native OpenCode discovery via `OPENCODE_CONFIG_DIR/skills/` symlinked to CAO's `SKILLS_DIR` (see [§5.1](#51-skill-delivery-native-discovery)). Replaces the baked-prompt catalog approach used for Q/Copilot — progressive loading keeps the system prompt lean. |
| `webfetch`, `websearch`, `codesearch` | `deny` | Network egress — high-trust default. Can be enabled via a future CAO category if a concrete need emerges. |

**Translation examples:**

| Input `allowedTools` | Output `permission:` dict |
|---|---|
| `["*"]` | All 13 OpenCode tools → `allow` |
| `["@builtin"]` | `bash`, `read`, `edit`, `write`, `grep`, `glob` → `allow` (or `ask`); `task`, `question`, `webfetch`, `websearch`, `codesearch` → `deny`; `todowrite`, `skill` → `allow` |
| `["execute_bash", "fs_read"]` | `bash`, `read` → `allow` (or `ask`); rest per defaults (edit/write/grep/glob → `deny`; non-vocabulary tools per hardcoded policy) |
| `["fs_*", "@cao-mcp-server"]` | All fs tools → `allow`; `bash` → `deny`; MCP handled in `opencode.json`; non-vocabulary tools per hardcoded policy |

## 10. Risks and Open Questions

### 10.1. TUI output patterns — RESOLVED

**Status: resolved 2026-04-20 via tmux probe.** OpenCode is a 24-bit-truecolor alt-screen
TUI. All five status markers captured:

| State | Marker | Source |
|---|---|---|
| IDLE | `ctrl+p commands` footer visible, no `esc interrupt` | Probe frame 01, 07 |
| PROCESSING | `esc interrupt` keybind hint in footer (+ `[■⬝]+` spinner) | Probe frame 02 |
| COMPLETED | `▣  <agent>  ·  <model>  ·  N.Ns` marker line | Probe frame 03 |
| WAITING_USER_ANSWER | `△  Permission required` (or `Always allow`) heading + `enter confirm` footer | Probe frame 05 |
| ERROR | fallback; no forced error captured (low priority) | — |

Concrete regexes and detection algorithm are now in
[§8.1](#81-regex-patterns-verified-from-probe-fixtures) and
[§8.3](#83-get_status--priority-order). Fixture captures live under
`/tmp/cao-opencode-probe-captures/` and should be moved into
`test/providers/fixtures/opencode_cli_*.txt` during implementation.

Behavior is independent of which model answers — OpenCode Zen's free tier is sufficient
for fixture capture; authenticated-provider behavior will not cause deviation in the TUI
layout or status markers.

### 10.2. First-run workspace trust prompt — RESOLVED

**Status: resolved 2026-04-20.** Verified by running `opencode --agent build` in a fresh
temp directory with an empty `OPENCODE_CONFIG_DIR` — no trust/welcome prompt appears.
Launch goes straight to the idle splash frame. No `_handle_startup_prompts()` loop is
needed in `initialize()`.

### 10.3. Project-local `opencode.json` override

**Severity: low, accepted risk, documented constraint.** OpenCode's config merge precedence
is[^5]:

1. Remote `.well-known/opencode`
2. Global `~/.config/opencode/opencode.json`
3. `OPENCODE_CONFIG` (custom file) ← _CAO writes here_
4. Project `opencode.json` (in cwd) ← _overrides CAO_
5. `.opencode/` dirs
6. `OPENCODE_CONFIG_CONTENT` (inline JSON env var)

If a user launches CAO in a directory with its own `opencode.json` containing conflicting
`agent.<name>.tools` or `tools` entries, CAO's MCP wiring could be silently overridden for
that agent.

**Decision:** out of scope for CAO to manage the user's project directory state. Document
the constraint in `docs/opencode-cli.md` troubleshooting; do not try to detect or warn at
launch time.

### 10.4. Tool vocabulary completeness — RESOLVED

**Status: resolved 2026-04-20.** Full tool list enumerated in [§9](#9-tool-vocabulary):
13 built-in tools (`read`, `write`, `edit`, `glob`, `grep`, `bash`, `task`, `question`,
`webfetch`, `websearch`, `codesearch`, `skill`, `todowrite`) plus per-MCP-server
`<servername>_*`. Translator mapping and per-tool default policy documented.

### 10.5. Exit command — RESOLVED

**Status: resolved 2026-04-20.** `/exit` typed in the input box exits the OpenCode TUI
cleanly (matching the Kiro idiom). `Ctrl+C` also works and is available as a fallback.
See [§8.5](#85-exit_cli).

### 10.6. `opencode mcp add` reliability for programmatic config writes

**Severity: low.** Docs reference the command but don't document its syntax
comprehensively[^3]. **Decision:** write `opencode.json` directly via the atomic
read-modify-write helper (`utils/opencode_config.py`) rather than shelling out — matches
how `cao install` already writes Kiro/Q JSON directly and avoids subprocess failure modes.

### 10.7. First-launch `node_modules` install (~57MB)

**Severity: low, mitigated by generous `initialize()` timeout.** Verified 2026-04-20: on
first launch in an empty `OPENCODE_CONFIG_DIR`, OpenCode writes `package.json`
(`@opencode-ai/plugin` pinned to the binary version) and runs `npm install` — ~57MB,
5–30s blocking before the TUI paints.

**Mitigation:** `initialize()` uses `timeout=120.0` (see [§8.2](#82-initialize)), so the
existing capture-pane polling loop naturally waits out the install without CAO needing
explicit pre-warm or version-detection logic. This is idiomatic for CAO — see
`src/cli_agent_orchestrator/providers/kimi_cli.py:349-354`.

**Sub-risk to probe at impl time:** concurrent assign-flow on cold cache. If a user runs
`cao install --provider opencode_cli` followed immediately by `cao launch --agents A,B,C`
before any `opencode` has ever executed against the CAO-owned `OPENCODE_CONFIG_DIR`, three
`opencode` processes race to `npm install` into the same directory. If this breaks, the
targeted fix is either (a) a lightweight npm-level pre-populate step in `cao install`
(just to produce `node_modules/`, not a full launch) or (b) a per-config-dir file lock
serializing the first launch. Defer until empirically observed.

## 11. File Touchpoints

Against the `cao-provider` skill checklist:

- [ ] `src/cli_agent_orchestrator/models/provider.py` — add `OPENCODE_CLI = "opencode_cli"`
- [ ] `src/cli_agent_orchestrator/models/opencode_agent.py` — new `OpenCodeAgentConfig`
- [ ] `src/cli_agent_orchestrator/providers/opencode_cli.py` — new provider class
- [ ] `src/cli_agent_orchestrator/providers/manager.py` — register elif branch
- [ ] `src/cli_agent_orchestrator/cli/commands/install.py` — new elif branch (emits `.md` + merges `opencode.json`)
- [ ] `src/cli_agent_orchestrator/cli/commands/launch.py` — add to `PROVIDERS_REQUIRING_WORKSPACE_ACCESS`
- [ ] `src/cli_agent_orchestrator/utils/opencode_permissions.py` — new translator helper
- [ ] `src/cli_agent_orchestrator/utils/opencode_config.py` — new config editor + `ensure_skills_symlink()` helper (see [§5.1](#51-skill-delivery-native-discovery))
- [ ] `src/cli_agent_orchestrator/constants.py` — `OPENCODE_CONFIG_DIR`, `OPENCODE_AGENTS_DIR`, `OPENCODE_CONFIG_FILE`
- [ ] `test/providers/test_opencode_cli_unit.py` — unit tests
- [ ] `test/providers/fixtures/opencode_cli_*.txt` — TUI output fixtures (from [§10.1](#101-tui-output-patterns))
- [ ] `test/e2e/conftest.py` — `require_opencode` fixture
- [ ] `test/e2e/test_*.py` — e2e test classes
- [ ] `docs/opencode-cli.md` — provider docs
- [ ] `README.md` — provider table
- [ ] `CHANGELOG.md` — new provider entry
- [ ] _(no `utils/tool_mapping.py` entry — native enforcement via frontmatter)_

## 12. References

[^1]: OpenCode CLI reference. https://opencode.ai/docs/cli
[^2]: OpenCode Agents. https://opencode.ai/docs/agents
[^3]: OpenCode MCP Servers. https://opencode.ai/docs/mcp-servers
[^4]: OpenCode Permissions. https://opencode.ai/docs/permissions
[^5]: OpenCode Config. https://opencode.ai/docs/config
[^sk]: OpenCode Skills. https://opencode.ai/docs/skills

## Implementation Plan

Multi-phase implementation plan derived from this design document. Phases are sequential;
tasks within a phase are independent.

### Phase 1: Foundation primitives

**Goal:** Lay down the independent building blocks (enum entry, constants, Pydantic model,
utility helpers, test fixtures) that the install and runtime phases will consume. Each
piece is net-new, additive, and testable in isolation.

**Acceptance Criteria:**

- New files pass `uv run mypy src/` (strict mode), `uv run black src/ test/`, `uv run isort src/ test/`
- `OPENCODE_CLI` enum value is importable from `cli_agent_orchestrator.models.provider`
- `OpenCodeAgentConfig` serializes to OpenCode-compatible frontmatter via `frontmatter.dumps()`
- Permission translator unit-tests pass for every CAO category in [§9](#9-tool-vocabulary)
- `opencode.json` editor unit-tests demonstrate idempotent read-modify-write and preservation of pre-existing user entries
- Fixture files exist for IDLE (splash + post-completion), PROCESSING, COMPLETED, WAITING_USER_ANSWER states (plain + ANSI variants)

**Depends on:** None

**Tasks:**

- **Register OPENCODE_CLI in ProviderType enum**
  - **Description:** Add `OPENCODE_CLI = "opencode_cli"` to `src/cli_agent_orchestrator/models/provider.py`. Update the `PROVIDERS` list in `src/cli_agent_orchestrator/constants.py` if it's centrally defined there.
  - **Acceptance Criteria:** `ProviderType.OPENCODE_CLI.value == "opencode_cli"`. `opencode_cli` accepted by `cao install --provider` flag's choice validation.

- **Add OpenCode path constants**
  - **Description:** Add to `src/cli_agent_orchestrator/constants.py`: `OPENCODE_CONFIG_DIR = Path.home() / ".aws" / "opencode_cli"`, `OPENCODE_AGENTS_DIR = OPENCODE_CONFIG_DIR / "agents"`, `OPENCODE_CONFIG_FILE = OPENCODE_CONFIG_DIR / "opencode.json"`.
  - **Acceptance Criteria:** Constants importable and resolve to `~/.aws/opencode/...` paths. Unit test asserts the three paths.

- **Create OpenCodeAgentConfig Pydantic model**
  - **Description:** New file `src/cli_agent_orchestrator/models/opencode_agent.py` modeled on `models/kiro_agent.py`. Fields: `description: str`, `mode: Literal["all", "primary", "subagent"] = "all"`, `permission: dict[str, str | dict] | None = None`. The prompt body is not a model field — it's written to the `.md` body via `frontmatter.dumps()` at install time.
  - **Acceptance Criteria:** Model validates correctly. `frontmatter.dumps(Post(body, **model.model_dump(exclude_none=True)))` produces valid OpenCode agent markdown. Unit test round-trips a sample config.

- **Create CAO → OpenCode permission translator**
  - **Description:** New file `src/cli_agent_orchestrator/utils/opencode_permissions.py`. Function `cao_tools_to_opencode_permission(allowed_tools: list[str], auto_approve: bool) -> dict[str, str]` implementing the full two-step algorithm in [§9](#9-tool-vocabulary). Step 1: expand CAO shorthand (`*` → everything including non-vocabulary tools allow; `@builtin` → `[execute_bash, fs_read, fs_write, fs_list]`; `@<mcp>` → skip, handled in `opencode.json`). Step 2: default all CAO-mappable OpenCode tools to `deny`, then flip to `allow` (or `ask` when `auto_approve=False`) for each expanded category. Apply hardcoded non-CAO-vocabulary policy: `task`, `question`, `webfetch`, `websearch`, `codesearch` → `deny` (always); `todowrite`, `skill` → `allow` (always). `*` in `allowedTools` overrides the non-vocabulary denies.
  - **Acceptance Criteria:** Unit tests cover each example row in [§9](#9-tool-vocabulary)'s translation-examples table: `["*"]` → all 13 tools `allow`; `["@builtin"]` → fs + bash `allow`/`ask`, task/question/webfetch/websearch/codesearch `deny`, todowrite/skill `allow`; `["execute_bash", "fs_read"]` → only bash+read allowed, rest per defaults; `["fs_*", "@cao-mcp-server"]` → fs tools allow, bash deny, MCP excluded from the returned dict. Both `auto_approve=True` and `auto_approve=False` variants tested (`allow` vs `ask` on the flipped tools).

- **Create opencode.json editor**
  - **Description:** New file `src/cli_agent_orchestrator/utils/opencode_config.py`. Functions: `upsert_mcp_server(name, config)`, `upsert_agent_tools(agent_name, mcp_names)`, `remove_agent_tools(agent_name)`, `read_config() -> dict`, `write_config(data: dict)`. Plain read-modify-write; parent directories created if missing. No file locking — concurrent `cao install` is not a supported scenario for v1 (see the non-goal note below and [§6](#6-mcp-wiring-and-per-agent-gating)).
  - **Acceptance Criteria:** Unit tests cover: fresh-file creation, idempotent re-upsert (same input yields same file), missing parent dir auto-created, existing user-authored entries preserved across upserts.

- **Port TUI fixtures into the test tree**
  - **Description:** Copy the 5 probe captures from `/tmp/cao-opencode-probe-captures/` into `test/providers/fixtures/` with names: `opencode_cli_idle_splash.txt`, `opencode_cli_idle_post_completion.txt`, `opencode_cli_processing.txt`, `opencode_cli_completed.txt`, `opencode_cli_permission.txt`. Include both plain and ANSI variants per fixture.
  - **Acceptance Criteria:** Fixtures committed. Each file has plain-text and `.ansi.txt` counterpart. Contents match the markers documented in [§8.1](#81-regex-patterns-verified-from-probe-fixtures).

### Phase 2: Install-time integration

**Goal:** `cao install --provider opencode_cli <agent>` produces a valid OpenCode agent
file under the CAO-owned config directory and merges MCP/tool-gating entries into the
shared `opencode.json`.

**Acceptance Criteria:**

- `cao install --provider opencode_cli <agent_name>` exits 0 and writes `~/.aws/opencode/agents/<agent>.md` with valid YAML frontmatter
- `cao install --provider opencode_cli <agent_with_mcp>` upserts the MCP server under top-level `mcp`, default-denies the server tools under top-level `tools`, and re-enables them per-agent under `agent.<name>.tools`
- Running `cao install` twice with the same agent produces identical files (idempotent)
- `OPENCODE_CONFIG=~/.aws/opencode/opencode.json OPENCODE_CONFIG_DIR=~/.aws/opencode_cli opencode agent list` shows the installed agent alongside built-ins
- MCP name collisions silently overwrite per [§6](#6-mcp-wiring-and-per-agent-gating) (users are expected to keep names globally consistent)

**Depends on:** Phase 1 (enum, constants, `OpenCodeAgentConfig`, permission translator, config editor, fixtures)

**Tasks:**

- **Add opencode_cli branch to cao install**
  - **Description:** In `src/cli_agent_orchestrator/cli/commands/install.py`, add `elif provider == ProviderType.OPENCODE_CLI.value:` after the Copilot branch. The branch: (1) calls `OPENCODE_AGENTS_DIR.mkdir(parents=True, exist_ok=True)`, (2) builds `OpenCodeAgentConfig` using `cao_tools_to_opencode_permission(allowed_tools, auto_approve)`, (3) writes the `.md` via `frontmatter.dumps()` with the body as `profile.system_prompt` (or `profile.prompt` fallback) — **not** `compose_agent_prompt(profile)`; the skill catalog is delivered via native OpenCode skill discovery (Phase 5), not baked into the prompt, (4) calls the config editor to upsert each `profile.mcpServers` entry and its per-agent tool gating. Safe-filename handling matches other branches.
  - **Acceptance Criteria:** Running against any existing CAO profile in `src/cli_agent_orchestrator/agent_store/` with `--provider opencode_cli` produces a valid OpenCode agent file. Agent body contains `profile.system_prompt`/`profile.prompt` only, with no `## Available Skills` block. `opencode agent list` with CAO-owned env vars shows the agent. Frontmatter fields exactly match the mapping table in [§4](#4-agent-profile-translation).

- **Unit tests for install branch**
  - **Description:** New test file `test/cli/test_install_opencode.py`. Cover: (a) fresh install creates agent .md + fresh opencode.json, (b) re-install is idempotent, (c) `--auto-approve` produces `permission: allow`, (d) agent with MCP servers produces correct `mcp`/`tools`/`agent.<name>.tools` blocks in opencode.json, (e) agent without MCP produces only agent.md (no opencode.json mutation needed), (f) existing user-authored entries in opencode.json are preserved across install.
  - **Acceptance Criteria:** All six scenarios have corresponding tests and pass. Test fixtures use `tmp_path` to avoid touching `~/.aws/`.

### Phase 3: Provider runtime

**Goal:** `OpenCodeCliProvider` is a registered, working provider class with verified
status detection against the fixtures and a passing manual smoke test.

**Acceptance Criteria:**

- `OpenCodeCliProvider` implements `BaseProvider` interface: `initialize`, `get_status`, `extract_last_message_from_script`, `exit_cli`, `cleanup`, `get_idle_pattern_for_log`
- Provider is created by `ProviderManager.create_provider()` when `provider_type == "opencode_cli"`
- `"opencode_cli"` is in `PROVIDERS_REQUIRING_WORKSPACE_ACCESS`
- Unit tests pass against all Phase 1 fixtures for every status state
- Manual smoke: `cao install developer --provider opencode_cli && cao launch --agents developer --provider opencode_cli` reaches IDLE and responds to a prompt
- `initialize()` uses `timeout=120.0` per [§8.2](#82-initialize)

**Depends on:** Phase 1 (fixtures, constants, models), Phase 2 (install path, needed to produce agents for the smoke test)

**Tasks:**

- **Implement OpenCodeCliProvider and register it**
  - **Description:** New file `src/cli_agent_orchestrator/providers/opencode_cli.py` modeled on `providers/kiro_cli.py`. Define module-level regex constants matching [§8.1](#81-regex-patterns-verified-from-probe-fixtures). Implement the priority-order algorithm from [§8.3](#83-get_status--priority-order) with position-aware stale-buffer guards. Implement `extract_last_message_from_script()` per [§8.4](#84-extract_last_message_from_script). `initialize()` builds the inline-env launch command from [§5](#5-config-isolation) (including `--model` when `profile.model` is set, per [§3.1](#31-integration-philosophy-native-first) exception), with `wait_until_status(timeout=120.0)`. `exit_cli()` returns `"/exit"`. Also add the elif branch in `providers/manager.py` and add `"opencode_cli"` to `PROVIDERS_REQUIRING_WORKSPACE_ACCESS` in `cli/commands/launch.py`.
  - **Acceptance Criteria:** `cao launch --provider opencode_cli` successfully creates a terminal that reaches IDLE. `ProviderManager.create_provider("opencode_cli", ...)` returns an `OpenCodeCliProvider` instance. A live-run test against a real `opencode` binary in a tmux session correctly detects IDLE → PROCESSING → COMPLETED transitions.

- **Unit tests for provider class**
  - **Description:** New test file `test/providers/test_opencode_cli_unit.py`. Use `unittest.mock.patch` to mock `tmux_client`. Cover: (a) `get_status()` returns correct enum for each fixture file, (b) `get_status()` correctly distinguishes IDLE-splash vs IDLE-post-completion vs PROCESSING vs COMPLETED vs WAITING_USER_ANSWER, (c) stale `esc interrupt` text followed by idle footer returns IDLE (not PROCESSING), (d) `extract_last_message_from_script()` returns the agent response and strips `Thinking:` preamble, (e) `initialize()` calls `wait_until_status` with `timeout=120.0`, (f) `exit_cli()` returns `"/exit"`, (g) regex patterns individually match expected substrings.
  - **Acceptance Criteria:** All seven scenarios have tests and pass. Coverage report shows the provider file at 90%+ line coverage.

### Phase 4: End-to-end validation and documentation

**Goal:** The provider is integration-tested against the canonical CAO multi-agent flow
and user-facing documentation exists.

**Acceptance Criteria:**

- E2E test using `examples/assign/` profiles (supervisor + 3 workers + 1 reporter) passes against OpenCode
- `docs/opencode-cli.md` exists with prerequisites, launch example, permission/MCP mapping notes, and known limitations (§10.3 project-local override)
- `README.md` provider table includes `opencode_cli` row
- `CHANGELOG.md` has a new entry under "Unreleased" announcing the provider

**Depends on:** Phase 3

**Tasks:**

- **E2E test with assign + handoff**
  - **Description:** Add `require_opencode` fixture to `test/e2e/conftest.py` that skips the test unless `opencode` binary is on PATH. Extend the existing assign/handoff e2e test file(s) with an OpenCode-provider variant that installs the three `examples/assign/` profiles with `--provider opencode_cli` and runs the full supervisor-assigns-workers-handoff-reporter flow. Marked `@pytest.mark.e2e` (excluded from default CI per `pyproject.toml`).
  - **Acceptance Criteria:** `uv run pytest -m e2e test/e2e/test_assign.py -k opencode` passes against a real `opencode` binary. Test validates all four orchestration modes: assign (non-blocking), handoff (blocking), send_message (inbox delivery), status transitions across concurrent terminals.

- **Write provider documentation**
  - **Description:** New file `docs/opencode-cli.md` modeled on `docs/kiro-cli.md`. Include: prerequisites (opencode binary, node_modules install on first launch is automatic), launch examples covering basic and --auto-approve, permission/tool mapping reference pointing at [§9](#9-tool-vocabulary) of this design, known limitations (project-local `opencode.json` in cwd can override CAO config per [§10.3](#103-project-local-opencodejson-override)), troubleshooting section for common failure modes (npm install timeout, auth-missing errors, first-launch delay).
  - **Acceptance Criteria:** Document renders correctly on GitHub. Cross-references in the provider table (see next task) resolve correctly.

- **Update README provider table**
  - **Description:** Add a row for `opencode_cli` to the provider table in `README.md`, including link to `docs/opencode-cli.md` and a one-line description.
  - **Acceptance Criteria:** Row appears in alphabetical/consistent ordering with other providers. Link works.

- **Update CHANGELOG**
  - **Description:** Add an entry under "Unreleased" in `CHANGELOG.md` announcing OpenCode provider support, following the style of previous provider additions (e.g., the Copilot CLI entry).
  - **Acceptance Criteria:** Entry follows CHANGELOG format. References the docs file and the feature design doc.

### Phase 5: Native skill discovery via symlink

**Goal:** Replace the per-agent baked skill catalog (currently omitted from the OpenCode
install branch in Phase 2) with OpenCode's native skills pipeline. After this phase,
agents installed under `--provider opencode_cli` discover every CAO skill through
OpenCode's own `skill` tool with progressive loading — no prompt-body catalog injection,
no `refresh_installed_agent_*` machinery needed for this provider.

**Acceptance Criteria:**

- `ensure_skills_symlink()` creates `OPENCODE_CONFIG_DIR/skills` as a symlink to
  `SKILLS_DIR` when the target path does not yet exist
- Re-calling `ensure_skills_symlink()` is a no-op when the symlink already points at the
  correct target (idempotent)
- `cao install --provider opencode_cli <agent>` calls `ensure_skills_symlink()` at least
  once per invocation so the symlink is in place before any OpenCode launch
- Live manual test: `opencode --agent developer` launched against a CAO-owned
  `OPENCODE_CONFIG_DIR` lists `cao-supervisor-protocols` and `cao-worker-protocols` when
  the agent invokes its `skill` tool
- No guard/repair logic for pre-existing non-symlink directories at the target path
  (deferred per [§5.1](#51-skill-delivery-native-discovery))

**Depends on:** Phase 2 (install branch — where the ensure call is wired in)

**Tasks:**

- **Add ensure_skills_symlink helper to utils/opencode_config.py**
  - **Description:** New function `ensure_skills_symlink() -> None` in
    `src/cli_agent_orchestrator/utils/opencode_config.py`. Logic: compute
    `target = OPENCODE_CONFIG_DIR / "skills"`; if `target` does not exist, ensure
    `OPENCODE_CONFIG_DIR` exists (`mkdir(parents=True, exist_ok=True)`) then call
    `target.symlink_to(SKILLS_DIR)`. If `target` already exists as a symlink to
    `SKILLS_DIR`, return. If `target` exists as anything else (non-symlink dir, symlink
    pointing elsewhere), log a warning and return without touching — per
    [§5.1](#51-skill-delivery-native-discovery) we do not try to repair user-owned state.
  - **Acceptance Criteria:** Three unit tests with `tmp_path`-scoped monkeypatched
    constants: (a) fresh target missing → symlink created, (b) target already the correct
    symlink → no-op, (c) target is a non-symlink directory → logged warning, no write.

- **Wire ensure_skills_symlink into cao install opencode_cli branch**
  - **Description:** In `src/cli_agent_orchestrator/cli/commands/install.py`, inside the
    `elif provider == ProviderType.OPENCODE_CLI.value:` branch added in Phase 2, call
    `ensure_skills_symlink()` before writing the agent `.md`. Order: mkdir agent dir →
    `ensure_skills_symlink()` → write agent file → upsert `opencode.json`.
  - **Acceptance Criteria:** Running `cao install <agent> --provider opencode_cli`
    against a fresh `OPENCODE_CONFIG_DIR` leaves `OPENCODE_CONFIG_DIR/skills` as a
    symlink to `SKILLS_DIR`. Running it a second time is idempotent and does not re-emit
    the symlink call into anything visible.

- **Note skill delivery in provider docs**
  - **Description:** In `docs/opencode-cli.md` (the doc added in Phase 4), add a short
    "Skills" section noting that CAO skills are exposed via OpenCode's native `skill` tool
    with progressive loading, not baked into the system prompt. Point at
    [§5.1](#51-skill-delivery-native-discovery) of this design doc for rationale.
  - **Acceptance Criteria:** Section exists, cross-reference resolves, tone matches rest
    of the provider doc.

### Phase 6: Rename config directory to `opencode` (minor)

**Goal:** Cosmetic rename of the CAO-owned OpenCode config directory from `opencode_cli`
to `opencode` — the provider identifier in the enum stays `opencode_cli` (user-facing),
but the on-disk directory name drops the `_cli` suffix for brevity and to match OpenCode's
own directory naming elsewhere (`~/.config/opencode/`).

**Acceptance Criteria:**

- `OPENCODE_CONFIG_DIR` resolves to `Path.home() / ".aws" / "opencode"`
- All references in design docs, provider docs, error messages, and test fixtures use the
  new path
- `ProviderType.OPENCODE_CLI` enum value remains `"opencode_cli"` — no user-visible flag
  change
- No data migration is performed — users who already ran Phase 1–5 flows need to
  re-install their agents under the new path (documented in `CHANGELOG.md`)

**Depends on:** Phases 1–5

**Tasks:**

- **Update constant and all references**
  - **Description:** Change `OPENCODE_CONFIG_DIR = Path.home() / ".aws" / "opencode_cli"`
    to `Path.home() / ".aws" / "opencode"` in
    `src/cli_agent_orchestrator/constants.py`. Grep and update all other references
    (design doc §4/§5, implementation plan task descriptions, `docs/opencode-cli.md`,
    README, test fixtures, inline-env launch-command samples). No logic change beyond the
    string swap.
  - **Acceptance Criteria:** No file under `src/`, `docs/`, `test/`, `README.md`, or
    `CHANGELOG.md` contains the old config path as a path component — only provider
    identifier enum/flag occurrences (`opencode_cli` without a following path separator)
    are allowed. All Phase 1–5 unit and e2e tests continue to pass after the rename.

- **Note the rename in CHANGELOG**
  - **Description:** Add a one-line note to the "Unreleased" CHANGELOG entry created in
    Phase 4 stating that the on-disk config directory is `~/.aws/opencode/` and that
    users who tried an earlier pre-release path must re-run `cao install` against the new
    location.
  - **Acceptance Criteria:** CHANGELOG note exists and explicitly calls out the path.
