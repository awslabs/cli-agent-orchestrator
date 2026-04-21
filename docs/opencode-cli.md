# OpenCode CLI Provider

## Overview

The OpenCode CLI provider enables CLI Agent Orchestrator (CAO) to work with **OpenCode**, a terminal-based AI assistant with a native agent system. OpenCode uses Markdown files with YAML frontmatter as its agent format — nearly identical to CAO's own profile format — making this integration especially clean.

For the full design rationale, architecture decisions, and permission-mapping tables, see [`docs/feat-opencode-provider-design.md`](feat-opencode-provider-design.md).

## Prerequisites

1. **OpenCode binary** — install from [opencode.ai](https://opencode.ai):
   ```bash
   npm install -g opencode-ai
   # or
   curl -fsSL https://opencode.ai/install | bash
   ```
2. **Node.js 18+** — required by OpenCode for its plugin system
3. **tmux 3.3+** — required by CAO for terminal management
4. **API credentials** — configure whichever model provider you want OpenCode to use (Anthropic, OpenAI, etc.) per [OpenCode's auth docs](https://opencode.ai/docs/auth)

### First-launch delay

On its **first ever launch** against a fresh CAO config directory (`~/.aws/opencode/`), OpenCode runs `npm install @opencode-ai/plugin` — roughly 57 MB of dependencies that take **5–30 seconds** to install. The TUI will appear blank until the install completes. This is expected; CAO's 120-second initialization timeout covers it automatically.

Subsequent launches complete in ~2 seconds.

## Quick Start

### 1. Install agent profiles

```bash
# Built-in profiles
cao install code_supervisor --provider opencode_cli
cao install developer --provider opencode_cli
cao install reviewer --provider opencode_cli

# Custom or example profiles
cao install examples/assign/data_analyst.md --provider opencode_cli
cao install examples/assign/report_generator.md --provider opencode_cli
```

### 2. Start the CAO server

```bash
uv run cao-server
```

### 3. Launch an agent

```bash
# Standard launch — shows tool summary and asks for confirmation
cao launch --agents developer --provider opencode_cli

# Skip confirmation prompt (restrictions still enforced)
cao launch --agents developer --provider opencode_cli --auto-approve

# Specify model override
cao launch --agents developer --provider opencode_cli --model anthropic/claude-sonnet-4-6

# Unrestricted (DANGEROUS) — agent can run any command
cao launch --agents developer --provider opencode_cli --yolo
```

Via HTTP API:

```bash
curl -X POST "http://localhost:9889/sessions?provider=opencode_cli&agent_profile=developer"
```

## Config Isolation

CAO runs OpenCode with `OPENCODE_CONFIG_DIR` and `OPENCODE_CONFIG` both pointing at `~/.aws/opencode/`, which is separate from the user's personal OpenCode config at `~/.config/opencode/`. This means:

- CAO-installed agents are visible in OpenCode's agent picker alongside the built-ins
- CAO's MCP wiring (`opencode.json`) never touches the user's personal setup
- Switching between `cao launch` and personal `opencode` usage is safe — they use independent config trees

Storage layout:

```
~/.aws/opencode/
├── opencode.json          # MCP servers + per-agent tool gating (written by cao install)
├── package.json           # written by opencode on first launch
├── node_modules/          # ~57 MB, written by opencode on first launch
└── agents/
    ├── code_supervisor.md
    ├── developer.md
    └── ...
```

## Permission and Tool Mapping

OpenCode enforces permissions natively via `permission:` YAML frontmatter in each agent file. CAO translates its `allowedTools` list to an OpenCode `permission:` dict at install time — **no entry in `utils/tool_mapping.py` is needed**.

For the complete CAO-category → OpenCode-tool mapping table and per-tool default policies, see [§9 of the design doc](feat-opencode-provider-design.md#9-tool-vocabulary).

### Summary

| CAO category | OpenCode tools enabled |
|---|---|
| `execute_bash` | `bash` |
| `fs_read` | `read` |
| `fs_write` | `edit`, `write` |
| `fs_list` | `glob`, `grep` |
| `fs_*` | `read`, `edit`, `write`, `glob`, `grep` |
| `@<mcp-server-name>` | Handled in `opencode.json` (not frontmatter) |

Tools not in any enabled category default to `deny`. The following tools have hardcoded policies regardless of `allowedTools`:

| Tool | Policy | Reason |
|---|---|---|
| `task` | deny | Sub-agents escape CAO's terminal tracking |
| `question` | deny | Blocks unattended flows indefinitely |
| `webfetch`, `websearch`, `codesearch` | deny | Network egress — opt-in only |
| `todowrite`, `skill` | allow | In-memory / additive, no side-effects |

Pass `--yolo` (or set `allowedTools: ["*"]` in the profile) to allow all 13 tools including the above.

### Auto-approve

```bash
cao launch --agents developer --provider opencode_cli --auto-approve
```

With `--auto-approve`, CAO writes `permission: allow` for all enabled tools into the agent frontmatter at install time. Without it, permission-gated operations raise OpenCode's native `△ Permission required` dialog — CAO detects this as `WAITING_USER_ANSWER` and halts the polling loop until a human confirms.

## Skills

CAO skills (e.g. `cao-supervisor-protocols`, `cao-worker-protocols`) are exposed to OpenCode agents through OpenCode's **native `skill` tool** with progressive loading — they are **not** baked into the agent's system prompt.

At `cao install --provider opencode_cli` time, CAO creates a symlink:

```
~/.aws/opencode/skills → ~/.aws/cli-agent-orchestrator/skills/
```

OpenCode auto-discovers `<OPENCODE_CONFIG_DIR>/skills/` and makes its contents available through the `skill` tool. Metadata (name, description) is listed up front; full skill bodies are loaded on demand. This means:

- Skill additions or removals under `~/.aws/cli-agent-orchestrator/skills/` take effect on the next OpenCode launch with no reinstall required.
- The agent's system prompt stays lean — only `profile.system_prompt`/`profile.prompt` is written to the `.md` body, with no catalog injection.
- CAO's `load_skill` MCP tool remains available as a second path to the same content (cross-provider parity).

For the full design rationale, see [§5.1 of docs/feat-opencode-provider-design.md](feat-opencode-provider-design.md#51-skill-delivery-native-discovery).

## Status Detection

The provider detects terminal state from the tmux capture buffer (ANSI-stripped):

| State | Marker |
|---|---|
| `IDLE` | `ctrl+p commands` footer, no `esc interrupt` |
| `PROCESSING` | `esc interrupt` footer keybind |
| `COMPLETED` | `▣ <agent> · <model> · Ns` completion marker followed by idle footer |
| `WAITING_USER_ANSWER` | `△ Permission required` or `△ Always allow` heading |
| `ERROR` | Fallback — no state marker matched |

## MCP Server Wiring

`cao install --provider opencode_cli` writes MCP server declarations into `~/.aws/opencode/opencode.json`:

- Each `mcpServers` entry from the agent profile is added under the top-level `mcp` key
- The server's tools are default-denied globally (`"<servername>*": false` under `tools`)
- Re-enabled per-agent under `agent.<profile_name>.tools`

`CAO_TERMINAL_ID` is **not** written to `opencode.json`. OpenCode spawns MCP subprocesses that inherit the tmux window's environment, so the terminal ID propagates naturally — the same mechanism Kiro uses.

## End-to-End Testing

```bash
# Install profiles first
cao install examples/assign/data_analyst.md --provider opencode_cli
cao install examples/assign/report_generator.md --provider opencode_cli
cao install developer --provider opencode_cli

# Start CAO server
uv run cao-server

# Run all OpenCode CLI e2e tests
uv run pytest -m e2e test/e2e/test_assign.py -k opencode -v

# Run a single test
uv run pytest -m e2e test/e2e/test_assign.py::TestOpenCodeCliAssign::test_assign_with_callback -v
```

The `test_assign_with_callback` test validates all four orchestration modes:
- **assign** (non-blocking): supervisor terminal created and stays IDLE
- **send_message** (inbox delivery): worker pushes result to supervisor inbox
- **status transitions**: IDLE → PROCESSING → COMPLETED across concurrent terminals
- **handoff** (blocking): inbox delivery triggers supervisor state transition

## Known Limitations

### Project-local `opencode.json` override

OpenCode's config merge precedence places a project-local `opencode.json` in the current working directory **above** `OPENCODE_CONFIG` (the CAO-managed file). If you `cao launch` in a directory that has its own `opencode.json` with conflicting `agent.<name>.tools` or `tools` entries, CAO's MCP wiring can be silently overridden for that agent.

**Workaround:** remove or rename the project-local `opencode.json` before launching CAO, or move it under `.opencode/` (a subdirectory OpenCode also searches but at a lower priority level).

### Scrolling enters tmux copy mode

When you scroll (mouse wheel or trackpad) inside a CAO-managed OpenCode terminal, tmux enters copy mode instead of scrolling the TUI conversation history. This is intentional.

CAO launches OpenCode with `OPENCODE_DISABLE_MOUSE=1`, which prevents OpenCode from requesting application mouse-reporting mode (`\x1b[?1000h`). Without that request, tmux does not forward scroll events to the OpenCode process — it intercepts them and enters copy mode instead.

The reason for this trade-off: if OpenCode owned scroll events, scrolling the conversation history would move the completion marker (`▣ <agent> · <model> · Ns`) off screen. The footer (`ctrl+p commands`, `esc interrupt`) is pinned to the bottom of the TUI and remains visible regardless of scroll position, so IDLE and PROCESSING detection are unaffected. But COMPLETED detection requires both the completion marker and the idle footer to be present simultaneously in the captured frame — if the marker has scrolled away, CAO never detects COMPLETED even after the agent finishes. Disabling mouse keeps the frame locked to the most recent render.

Press `q` or `Escape` to exit copy mode. If you need to read earlier conversation history, use the `get_output` API endpoint or the `/terminals/<id>/output` endpoint to retrieve the full captured log.

### `opencode.json` concurrent writes

Parallel `cao install --provider opencode_cli` invocations (e.g., from a batch script) can race on the shared `~/.aws/opencode/opencode.json` file. The second writer may clobber the first's agent entry. **Sequential installs are safe.** File locking is deferred to a future release.

## Troubleshooting

### First-launch blank TUI (5–30 seconds)

OpenCode installs `@opencode-ai/plugin` into `~/.aws/opencode/node_modules/` on the first launch. The terminal will appear blank until `npm install` completes. CAO's 120-second initialization timeout covers this automatically.

To pre-populate `node_modules/` before the first CAO launch (optional):
```bash
OPENCODE_CONFIG_DIR=~/.aws/opencode opencode --help
```

### "Unknown provider" error from the server

Ensure the CAO server running on port 9889 is the **dev version**, not the pre-installed binary:
```bash
# Kill any stale installed binary
pkill -f 'cao-server'
# Start the dev server
uv run cao-server
```

### Authentication / model errors

OpenCode itself handles model authentication. Verify your credentials are set for the model provider you want to use. Check `~/.config/opencode/opencode.json` (your personal config) for provider API keys, or set them via environment variables before launching.

### Permission prompt blocking an automated flow

If OpenCode raises `△ Permission required` and your flow is blocking:
1. Re-install the agent with `--auto-approve` to bake `permission: allow` into its frontmatter
2. Or respond to the prompt manually in the tmux window
3. Or use `--yolo` to disable all restrictions **(DANGEROUS — allows any command including `aws`, `rm`, `curl`)**

### Status stuck as `PROCESSING`

This can happen if:
- OpenCode launched but the TUI hasn't painted yet (transient — the poller recovers)
- A `node_modules` install is still in progress (wait up to 120s)
- The `opencode` binary isn't on PATH in the tmux window's shell (check `echo $PATH` inside tmux)
