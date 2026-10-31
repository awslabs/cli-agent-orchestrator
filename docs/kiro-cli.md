# Kiro CLI Provider

## Overview

The Kiro CLI provider enables CLI Agent Orchestrator (CAO) to work with **Kiro CLI**, an AI-powered coding assistant that operates through agent-based conversations with customizable profiles.

## Quick Start

### Prerequisites

1. **AWS Credentials**: Kiro CLI authenticates via AWS
2. **Kiro CLI**: Install the CLI tool
3. **tmux**: Required for terminal management

```bash
# Install Kiro CLI
npm install -g @anthropic-ai/kiro-cli

# Verify authentication
kiro-cli --version
```

### Using Kiro CLI Provider with CAO

```bash
# Start the CAO server
cao-server

# Launch a Kiro CLI-backed session (agent profile is required)
cao launch --agents developer --provider kiro_cli --engine v2
```

Via HTTP API:

```bash
curl -X POST "http://localhost:9889/sessions?provider=kiro_cli&agent_profile=developer"
```

**Note**: Kiro CLI requires an agent profile — it cannot be launched without one.

`--engine v2` is explicit but optional because v2 is the default. `--engine v3`
selects Kiro's v3 engine (`kas`, its earlier CAO name, is accepted as an alias
and stored as `v3`). v3 is opt-in and **not yet a supported configuration**; see
[Kiro v3 (preview)](#kiro-v3-preview) below.

## Kiro v3 (preview)

> **Status: preview, pending the operator checklist.** The v3 launch path below is
> built and unit-tested, but CAO does not claim v3 support until a live operator
> checklist has passed on a supported kiro-cli version. Until then, use `v2` for
> real work.

**Why it matters.** Kiro ships a second agent engine, v3, and offers to switch
users to it. On v3 an agent file in v2's format is skipped and Kiro starts its
own `Default` agent, which has read, write, shell and web tools. So CAO only
launches v3 when it can prove the agent's limits before any task is typed.

### Quick start

```bash
# Write one agent file that both engines are meant to load
cao install reviewer --provider kiro_cli --engine v3

# Launch it on v3
cao launch --agents reviewer --provider kiro_cli --engine v3
```

`--engine v2` writes a v2-only file again. A reinstall without `--engine`, and
an agent-plugin MCP refresh, keep whichever format is on disk.

### What CAO checks

Before anything is allocated:

- the kiro-cli version is one CAO supports (see below);
- the agent is not unrestricted (`*`);
- the profile's `kiroConfig.effort`, if set, is a plain level such as `high`;
- no `KIRO_HOME` other than `~/.kiro` reaches the launch;
- the installed agent file on disk is exactly what `cao install --engine v3`
  writes for its grant, and that grant fits the launch;
- the launch directory and its parents have no `.kiro/` configuration v3 would
  load (see *Workspace trust*).

After kiro-cli starts, and before any task is typed:

- no dialog is on screen (CAO never answers the workspace-trust dialog);
- the header names the agent (as on v2);
- the workspace check runs again;
- CAO reads the agent file's grant again (it must still fit the launch), then
  types `/tools` and `/mcp` and reads both panels: every tool tag must be one
  that grant implies, every MCP tool must belong to a granted server, and
  exactly the configured MCP servers must be running, each with a tool.

Only then does the terminal accept input. If any check fails, no task is typed
and the window is closed.

The launch command is:

```bash
/usr/bin/env -u ASBX_KIRO_MANDATORY_MCPS -u KIRO_AGENT_CONFIG_DIR -u KIRO_AGENT_ENGINE -u KIRO_HOME \
  kiro-cli chat --agent-engine v3 --trust-workspace --trust-all-tools [--model m] [--effort e] --agent <name>
```

`--effort` comes from the profile's `kiroConfig.effort`, when set.

### Supported kiro-cli versions

| kiro-cli | v3 engine | Status |
|---|---|---|
| 2.35.1 | `@kiro/agent` 0.66.70 | measured |
| 2.36.0 | `@kiro/agent` 0.66.70 | pending operator verification: refused at launch unless the operator opts in (below) |

Any other version refuses v3; v2 is not affected. A new kiro-cli release needs a
CAO update before v3 runs on it.

### Pending versions (operator only)

A version marked *pending operator verification* has a schema CAO read from the
installed files but has not yet measured on a live run. It is refused by default
(`kiro_v3_unverified_version`). The setting below exists for one purpose: so an
operator can run the live checklist on that version. **Leave it off otherwise.**

```json
{ "kiro": { "v3_allow_unverified_versions": true } }
```

- It goes in `settings.json` only. No environment variable or API call sets it,
  and only JSON `true` turns it on. Provisional (#1167).
- It admits a pending version and nothing else. A version that isn't in the
  table still refuses, and every other check on this page still runs in full.
- Each launch it admits logs a warning in the server log. The launch response
  carries a `launch_notes` entry naming the version, and `cao launch` prints it.

### Workspace trust

CAO passes `--trust-workspace` only when no `.kiro/` in the launch directory or
its parents holds configuration. Allowed: `steering/`, `specs/`, `skills/` and
`AGENTS.md`. Refused: `agents/`, `hooks/`, `settings/`, `workflows/`,
`extensions/`, `powers/`, any other entry, and any symlink. Your own `~/.kiro`
is not checked.

### When a v3 launch is refused

The message never names a path on the server (it may be a shared server); the server log does.

| Message starts with | What it means | Fix |
|---|---|---|
| `kiro_v3_unsupported_version` | kiro-cli is a version CAO has no schema for | use a supported version, or `--engine v2` |
| `kiro_v3_unverified_version` | the version is listed but not yet operator-verified | `--engine v2` until it is verified (operators running the checklist: see *Pending versions*) |
| `kiro_v3_unrestricted` | the agent or launch allows every tool (`*`) | restrict `allowedTools` |
| `kiro_v3_home_unsupported` | `KIRO_HOME` points somewhere other than `~/.kiro` | unset it for the launch |
| `kiro_v3_not_installed` | no agent file for this agent | `cao install <name> --provider kiro_cli --engine v3` |
| `kiro_v3_agent_unreadable` | something that can't be read as a file is where the agent file goes (a directory, a symlink loop, no permission) | remove it, then `cao install <name> --provider kiro_cli --engine v3` |
| `kiro_v3_bad_effort` | the profile's `kiroConfig.effort` is not a plain level such as `low`, `medium` or `high` | fix `kiroConfig.effort`, or `--engine v2` |
| `kiro_v3_bad_cwd` | CAO could not read the launch directory to check it, or the pane's own directory at the re-check after startup | launch from an existing directory |
| `kiro_v3_invalid:<check>` | the agent file on disk is not CAO's v3 render (a v2 file, a hand edit, or Kiro's own upgrader rewrote it) | `cao install <name> --provider kiro_cli --engine v3`; the message names any `.bak` copy Kiro left |
| `kiro_v3_workspace_config:<entry>` | the workspace's `.kiro/` (or a parent's) holds configuration v3 would load, an entry CAO does not know, a symlink, or can't be read | remove it, use a worktree without it, or `--engine v2` |
| `kiro_v3_trust_dialog` | kiro-cli asked to trust the workspace anyway, including when that dialog was declined and Kiro exited | report it; CAO never answers that dialog |
| `kiro_v3_startup_dialog` | kiro-cli showed a dialog or a selector at startup that CAO doesn't answer: one it has no pattern for, the approval picker, or v2's "Switch to V3" offer | report it with a screenshot |
| `kiro_v3_tools_mismatch` | `/tools` shows a tag or MCP tool the grant does not allow, shows no tools, or shows a layout CAO can't read (a clipped list too); the `/tools` palette didn't appear; the launch allows every tool; or the agent file is no longer a v3 universal file, or its grant no longer fits the launch | reinstall; if it persists, report it |
| `kiro_v3_mcp_not_ready` | `/mcp` shows an extra, failed or missing server, one still loading after about 20 s, or a layout CAO can't read; the `/mcp` palette didn't appear; or the prompt didn't come back after the panels | check that the server starts on its own, then retry |
| `kiro_v3_mcp_not_ready` (ungranted servers) | the agent file configures an MCP server its grant doesn't name | `cao install <name> --provider kiro_cli --engine v3` |

### FAQ

**Will my v2 agents change?** No. Only `cao install --engine v3`, or a profile
that says `engine: v3`, writes the universal file; every other install is
byte-identical to before.

**Can I use `hooks`, `toolsSettings` or `toolAliases` on v3?** Not yet: CAO
refuses to install such a profile for v3 rather than approximate it.

**Does CAO answer Kiro's approval prompt on v3?** No. It reads as waiting for an
answer, for you or a supervisor.

## Features

### Status Detection

The Kiro CLI provider detects terminal states by analyzing ANSI-stripped output:

- **IDLE**: Agent prompt visible (legacy `[profile_name] >` or new TUI `ask a question, or describe a task`), no response content
- **PROCESSING**: No idle prompt found in output (agent is generating response)
- **COMPLETED**: Green arrow (`>`) response marker (legacy) or `▸ Credits:` marker (TUI) present + idle prompt after it
- **WAITING_USER_ANSWER**: Permission prompt visible (`Allow this action? [y/n/t]:`)
- **ERROR**: Known error indicators present (e.g., "Kiro is having trouble responding right now")

Status detection priority: no prompt → PROCESSING → ERROR → WAITING_USER_ANSWER → COMPLETED → IDLE.

The provider supports both the legacy UI prompt format and the new TUI format for all status detection and message extraction.

### Dynamic Prompt Pattern

The provider supports two prompt formats:

**Legacy UI** (used with `--legacy-ui` flag):

```
[developer] >          # Basic prompt
[developer] !>         # Prompt with pending changes
[developer] 50% >      # Prompt with progress indicator
[developer] λ >        # Prompt with lambda symbol
[developer] 50% λ >    # Combined progress and lambda
```

Pattern: `\[{agent_profile}\]\s*(?:\d+%\s*)?(?:\u03bb\s*)?!?>\s*`

**New TUI** (default in latest Kiro CLI):

```
code_supervisor · claude-opus-4.6-1m · ◔ 1%
 ask a question, or describe a task ↵
```

The new TUI idle state is detected by the `ask a question, or describe a task` pattern, and completion is detected by the `▸ Credits:` marker followed by an idle prompt. The provider launches in TUI mode by default and auto-detects which format is active.

### Message Extraction

The provider uses a dual-path extraction strategy:

**Legacy mode** (green arrow markers):
1. Strip ANSI codes from output
2. Find all green arrow (`>`) markers (response start)
3. Take the last one
4. Find the next idle prompt after it (response end)
5. Extract and clean text between them

**TUI mode** (separator + Credits markers):
1. Find the last `▸ Credits:` line (response end marker)
2. Find the last separator (`────`) before Credits (response start area)
3. Extract text between separator and Credits
4. Skip the first paragraph (user message echo)
5. Clean remaining text (ANSI, escape sequences, control characters)

The provider tries legacy extraction first; if no green arrows are found, it falls back to TUI extraction. This is backward compatible with `--legacy-ui` wrapper scripts.

### Permission Prompts

Kiro CLI shows `Allow this action? [y/n/t]:` prompts for sensitive operations (file edits, command execution). The provider detects these as `WAITING_USER_ANSWER` status. Unlike Claude Code, Kiro CLI does not have a trust folder dialog.

### Tool Restrictions

`cao install --provider kiro_cli` writes the profile's resolved `allowedTools` into the agent JSON twice: as `allowedTools` (Kiro's "run without a prompt" list) and, translated to Kiro's own tool names, as `tools`, which is what the agent can use at all. CAO launches `--trust-all-tools`, so `allowedTools` restricts nothing; `tools` does. A restricted `code_supervisor` gets `read`, `glob`, `grep`, `knowledge` and `@cao-mcp-server` and has no shell to call; `@builtin` becomes the harmless chrome (`goal`, `introspect`, `todo_list`) because in Kiro a bare `@builtin` means every built-in, shell included. `subagent` and `use_aws` follow `execute_bash`, `code` follows `fs_write`, `knowledge` follows `fs_read`. An unrestricted policy writes `tools: ["*"]`; a profile with its own `tools` list keeps it.

Enforcement is install-time: changing a profile's policy takes effect at the next `cao install`, and `--allowed-tools` or a role override at launch does not change the installed agent. A profile installed before CAO wrote `tools` still carries `["*"]`; `cao launch` and the server warn when they find one, and treat that terminal as unrestricted until it is reinstalled. See [Tool Restrictions](tool-restrictions.md).

Legacy-policy inspection preserves the `/` and `\` to `__` filename mapping
and resolves both the agent directory and the target before reading. The target
must be beneath that directory: an escaping symlink or a link to the directory
itself raises an error rather than being treated as a missing policy. Symlinks
to files within the agent directory and a symlinked agent directory remain
supported. These policy-path refusals return HTTP 400 from session creation,
terminal creation, and run-step requests; missing sessions or terminals retain
their HTTP 404 responses.

## Configuration

### Agent Profile (Required)

Kiro CLI always requires an agent profile. CAO passes it via:

```
kiro-cli chat --agent {profile_name}
```

The profile name determines the prompt pattern used for status detection. Built-in profiles include `developer` and `reviewer`.

### Launch Command

The provider launches using kiro-cli's default UI:

```
kiro-cli chat --agent-engine v2 --agent developer
```

The provider auto-detects whether the terminal is in legacy or TUI mode and uses the appropriate detection patterns, so both sets of patterns remain supported (a wrapper script may still put the terminal in legacy mode).

**CAO never passes `--legacy-ui` itself, and there is no legacy-UI retry on timeout.** The flag is mutually exclusive with `--agent-engine=v2` — kiro-cli exits with `Conflicting options: --legacy-ui cannot be used with --agent-engine=v2` — and on its own it implicitly selects the **v1 engine, which exposes no MCP tools to the model**. Because CAO's orchestration surface (`assign`, `handoff`, `report_outcome`, …) is delivered entirely over MCP, a v1 launch produces an agent that starts cleanly and then cannot orchestrate at all. The failure is silent: the MCP server still boots and logs `✓ cao-mcp-server loaded`, while every one of its tools is missing from the agent's tool list. A startup timeout therefore raises instead of retrying.

## Implementation Notes

- **ANSI stripping**: All pattern matching operates on ANSI-stripped output for reliability
- **Green arrow pattern**: `^>\s*` matches the start of agent responses (after ANSI stripping)
- **Generic prompt pattern**: `\x1b\[38;5;13m>\s*\x1b\[39m\s*$` matches the purple-colored prompt in raw output (used for log monitoring)
- **Error detection**: Checks for known error strings like "Kiro is having trouble responding right now"
- **Multi-format cleanup**: Extraction strips ANSI codes, escape sequences, and control characters
- **Exit command**: `/exit` via `POST /terminals/{terminal_id}/exit`

### Status Values

- `TerminalStatus.IDLE`: Ready for input
- `TerminalStatus.PROCESSING`: Working on task
- `TerminalStatus.WAITING_USER_ANSWER`: Waiting for permission confirmation
- `TerminalStatus.COMPLETED`: Task finished
- `TerminalStatus.ERROR`: Error occurred

## End-to-End Testing

The E2E test suite validates handoff, assign, and send_message flows for Kiro CLI.

### Running Kiro CLI E2E Tests

```bash
# Start CAO server
uv run cao-server

# Run all Kiro CLI E2E tests
uv run pytest -m e2e test/e2e/ -v -k kiro_cli

# Run specific test types
uv run pytest -m e2e test/e2e/test_handoff.py -v -k kiro_cli
uv run pytest -m e2e test/e2e/test_assign.py -v -k kiro_cli
uv run pytest -m e2e test/e2e/test_send_message.py -v -k kiro_cli
uv run pytest -m e2e test/e2e/test_supervisor_orchestration.py -v -k KiroCli -o "addopts="
```

## Troubleshooting

### Common Issues

1. **"Agent profile required" Error**:
   - Kiro CLI cannot be launched without an agent profile
   - Always specify `--agents` when launching: `cao launch --agents developer --provider kiro_cli`

2. **Permission Prompts Blocking**:
   - Kiro CLI shows `[y/n/t]` prompts for operations
   - The provider detects these as `WAITING_USER_ANSWER`
   - In multi-agent flows, the supervisor or user must handle these

3. **Authentication Issues**:
   ```bash
   # Verify AWS credentials
   aws sts get-caller-identity
   # Set credentials via environment
   export AWS_ACCESS_KEY_ID=...
   export AWS_SECRET_ACCESS_KEY=...
   export AWS_DEFAULT_REGION=...
   ```

4. **Prompt Pattern Not Matching**:
   - The provider supports both legacy (`[name] >`) and new TUI (`ask a question, or describe a task`) formats
   - TUI mode is the default; the provider auto-detects which format is active
   - Check with: `kiro-cli chat --agent your_profile`
   - Do **not** add `--legacy-ui` via a wrapper script to work around a detection problem: it drops kiro to the v1 engine, which serves no MCP tools, so the agent will start but be unable to `assign`/`handoff`/`report_outcome`

5. **JSON-Only Agent Profiles (AIM-Installed)**:
   - Agents installed via AIM (Agent Install Manager) may only have `.json` profiles (e.g., `~/.kiro/agents/librarian/agent-spec.json`)
   - CAO's `load_agent_profile()` primarily scans for `.md` files
   - If the agent is not found, CAO gracefully falls back — kiro-cli resolves `.json` profiles natively
   - As a workaround, you can create a stub `.md` file alongside the `.json` profile

6. **Agent Reports "No Such Tool" for MCP Tools**:
   - Symptom: startup logs show `✓ cao-mcp-server loaded in N s`, but the agent says it has no `assign`/`handoff`/`report_outcome` tool
   - Cause: the session is running the **v1 agent engine**, which does not expose MCP tools. Almost always because `--legacy-ui` reached the command line (it implies v1), e.g. via a wrapper script
   - Check: `kiro-cli mcp list` from inside the session — `cao-mcp-server` will be absent from the enabled servers
   - Fix: remove `--legacy-ui`; CAO pins `--agent-engine v2` on its own

7. **"Kiro agent '<name>' was not found" or "Kiro started agent 'kiro_default' instead of '<name>'"**:
   - kiro-cli does not refuse an `--agent` it cannot load. It prints `agent "<name>" not found, using "kiro_default"` and starts its built-in default agent, which has every built-in tool (shell, write, `use_aws`). CAO refuses such a launch instead (#1074).
   - Before launch, CAO looks for the agent in `<working directory>/.kiro/agents/`, in the directory `cao install` writes (`~/.kiro/agents/` unless `CAO_AGENTS_DIR` is set), in `~/.kiro/agents/`, and in `$KIRO_HOME/agents/` when `KIRO_HOME` is an absolute path (from the launch env, the session env or the server env). Any of these counts: a `<name>.json`, a `<name>/` directory holding a JSON spec (the AIM layout above), or a spec whose `name` is `<name>`. If none is found, the create fails with `400` before any terminal exists. The error gives the remedy; the directories CAO searched are in the server log, not in the response. Only JSON specs count: kiro-cli does not load `.md` agent files, so an agent that exists only as `<name>.md` is refused as `agent_not_found` until it is installed for `kiro_cli`. The workspace is the launch's own working directory, not its parents (kiro-cli does not walk up either), so the same profile can pass from one directory and be refused from another; the server log names the directories searched for each refused launch, the launch directory included.
   - kiro-cli itself reads the `cao install` directory only when it is `~/.kiro/agents/` or `$KIRO_HOME/agents/`. With `CAO_AGENTS_DIR` set in the server's environment *before it starts*, a launch passes the check above and is then refused at the header check below, after kiro-cli has started. `cao-server --agents-dir` is different: it sets `CAO_AGENTS_DIR` after `cao install`'s own directory is already bound from the default, so the check above never searches it, and such a launch is refused early as `agent_not_found` -- the `cao install` remedy below does not help that case.
   - After kiro-cli starts, and before any task is typed, every TUI header row on the screen (a row that starts with `<agent> · <model> · ◔ N%` at the left edge, with up to three short columns between the model and the gauge, such as the effort and thinking settings in `<agent> · <model> · thinking · low · ◔ N%`) must name the agent, and the screen must not show kiro-cli's fallback notice for that agent (`agent "<name>" not found, using "kiro_default"` on v2; on v3 `… using "default"`, after `not found`, `needs upgrading for this agent engine` or `has an invalid config`), whatever the header shows. A notice naming another agent does not refuse on its own. If any header row names another agent, no header row can be read, or the notice for the requested agent is shown, no task is typed and CAO closes the terminal's window -- or, if that close cannot be confirmed, refuses input to it instead. A synchronous create fails with `400`. A deferred create (assign, handoff, or `cao launch --headless` with a task) records the failure on the terminal, so `GET /terminals/{id}` reads `ERROR`, and closes the window (or refuses input, per above) even when the terminal has no caller and its record is kept for an external owner. An agent file that kiro-cli cannot parse ends here; run `kiro-cli agent list` and look for `Error:` lines. A header whose agent name is elided (`…`) or wrapped, by a narrow pane or a long name, names no agent: it is read again and then refused as `header_not_found`. So is a pane left in Kiro's plan mode, whose header shows `Plan · <model>` instead of the agent's name, except that it is refused as `identity_mismatch`.
   - The header proves the agent's *name*, not which definition of it is running. A `.kiro/agents/<name>.json` in the launch directory wins over the installed one (kiro-cli prints `WARNING: Agent conflict for <name>. Using workspace version.`), so a launch that passes the check can run a workspace definition with more tools than the installed profile. Refusing that for bounded launches is tracked as a follow-up.
   - A Kiro terminal takes typed input only once CAO has recorded that its header check passed. Until then `POST /terminals/{id}/input`, `/key` and `/exit` and `cao session send <session> <message> --terminal <id>` are refused with `409`, and inbox delivery does not type into it: a queued message waits while the terminal is still starting, and is delivered once the check passes. While the terminal is still starting the refusal is temporary (`has not passed its Kiro identity check yet`). The web terminal (`/terminals/{id}/ws`) drops the attached pane's own keystrokes the same way, and typing works once the check passes, without reconnecting; attaching to a pane whose runtime was already reclaimed is refused outright. **But `/ws` is a full tmux (or herdr) client of the whole session it attaches to** -- its prefix keys, the mouse, or the tmux command prompt can still switch to and type into another pane in that session, including an unverified one. That is not new to this check (`/ws` had no such gate before it either); it is tracked as a follow-up to scope the web terminal to only the pane it attached to. If startup ended without the record -- the header was refused, startup timed out before the header was read (a slow MCP server, say), or `cao-server` restarted mid-start -- the window is closed as above, input stays refused, and the failure in `GET /terminals/{id}` carries `untrusted_runtime: true`. Attaching the web terminal to such a terminal is refused once its window is closed. Delete the terminal and launch it again. A Kiro terminal whose check passed before a `cao-server` restart still accepts a re-sent task.
   - Kiro terminals launched by a CAO version before this check carry no record and are not re-verified: they keep accepting input across the upgrade, unless they carry a startup failure. One whose initial message had not been delivered when `cao-server` restarted for the upgrade keeps its window, but the restart records that failure, so input to it is refused. Relaunch them to get the check.
   - Fix: `cao install <name> --provider kiro_cli`, then retry. Kiro's built-in agents (`kiro_default`, `kiro_planner`, and `kiro_help`, which kiro-cli 2.35 ships as `kiro_guide`) have no agent file, so they cannot be launched through CAO. The v3 engine's fallback agent, which its header shows as `Default` and its notice names `default`, is refused like `kiro_default`, even when requested by name and even with an agent file of that name (#1167). `cao launch --memory` on Kiro also needs `cao install memory_manager --provider kiro_cli`; without it the response reports the sidecar as `refused`.

## kiro-cli 2.11+ Notes

Starting with kiro-cli 2.11, the TUI changed how it accepts pasted input and how
it renders the processing indicator. CAO's kiro_cli provider handles these:

- **Paste submission**: kiro 2.11 needs **two** Enter keystrokes after a
  bracketed paste to submit the message (first Enter finalizes the paste, second
  submits). The provider sets `paste_enter_count = 2` and `paste_submit_delay =
  1.0s`. Older kiro versions submitted on a single Enter.
- **Processing indicator**: kiro 2.11 replaced `"Kiro is working"` with
  `"Thinking..."` (with an optional `"(esc to cancel)"` suffix). The provider
  matches either variant in `TUI_PROCESSING_PATTERN`.
- **Idle placeholder always visible**: The `"ask a question or describe a task"`
  placeholder text remains in the raw buffer even during processing. The
  provider's Check 6 in `get_status()` requires a bordered response box (two
  separators + ≥2 content lines between them) before a bare idle-prompt match is
  treated as COMPLETED — otherwise the worker would be torn down within seconds
  of a task being sent.
- **Spinner animation floods the event bus**: kiro 2.11's TUI redraws a braille
  spinner (`⠋⠙⠹⠸⠼⠴⠦⠧`) roughly 10 times per second while an agent is thinking.
  Each redraw is a separate FIFO write. Without coalescing, that produces
  thousands of `terminal.{id}.output` events per turn — enough to overflow the
  shared async queue and drop the worker's real state transitions along with
  the animation noise. CAO's FIFO reader batches chunks arriving within a
  50ms window into one publish (`_COALESCE_WINDOW`), reducing publish rate
  ~20x during bursts. Consumers see the same bytes in the same order; only
  the event boundaries change. If handoff/assign start hanging on a newer
  kiro release, check whether the animation frame rate increased beyond what
  50ms can absorb.

If you upgrade kiro-cli and handoffs stop working (worker gets killed
prematurely, or the task sits unsent in the input box), check whether the
paste-submit behavior or processing-indicator text changed in the new version
and update the provider constants accordingly.

### Agent-plugin MCP working directory

Its MCP config format has no working-directory key (checked against the vendor's own MCP documentation, 2026-09-16), so CAO carries an agent plugin's declared `cwd` by launching the server through `/bin/sh -c 'cd -- "$1" && shift && exec "$@"'`. `exec` replaces the shell, the environment passes through, and argument boundaries survive because each argument stays a separate argv element. On a host with no `/bin/sh` such a server is skipped with `mcp.cwd_unsupported` rather than started in the wrong directory.
See [Agent Plugins](agent-plugins.md) for the full per-provider table.
