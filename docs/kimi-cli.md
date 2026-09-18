# Kimi CLI Provider

## Overview

The Kimi CLI provider drives Moonshot AI's coding agent CLI. There are **two
incompatible TUIs** in the wild under the same `kimi` command name, and CAO
supports both from one provider:

| | **Legacy** — `kimi-cli` (MoonshotAI) | **Kimi Code** — `agent-core-v2` |
|---|---|---|
| Prompt | `✨` / `💫` emoji, inline | boxed composer (`╭─ │ > │ ╰─`) |
| Response bullet | `•` (U+2022) | `●` (U+25CF, colour 253) |
| Reasoning style | grey 244 + italic | grey 244 + italic |
| Working indicator | bare moon phase (`🌕`) | braille (`⠙ working…`) |
| Idle tip | — | rotating `🌕 · Tip: …` row |
| Agent profile | YAML, `extend: default` | Markdown, `${base_prompt}` |
| MCP config | `--mcp-config '<json>'` | **no flag** — user-level `mcp.json` only |
| Unattended flag | `--yolo` (auto-approves all confirmations) | `--auto` (Never Ask) |
| Working directory | per-worker temp dir (`cd`) | the terminal's real cwd |
| Concurrency | one instance per directory (lock) | no per-directory lock |
| Timeout config | mutates `~/.kimi/config.toml` | launch-scoped `KIMI_MCP_*_TIMEOUT_MS` |

Dialect selection is capability-based: CAO never infers a dialect from a
version number. The CODE path was validated end-to-end against Kimi Code
**0.43.1**. (A later local install, Kimi Code 2.0.0, still exposes the same
capability flags in `--help`; that is a limited observation, not full
2.0.0 TUI/E2E support.)

## Prerequisites

- **Kimi CLI**: legacy — `brew install kimi-cli` or `uv tool install kimi-cli`.
  Kimi Code — installs itself under `~/.kimi-code/bin/kimi`.
- **Authentication**: legacy — `kimi login`. Kimi Code — `kimi login`
  (device-code flow).
- **tmux 3.3+**

Verify installation:

```bash
kimi --version
```

## Quick Start

```bash
# Authenticate
kimi login

# Launch with CAO
cao launch --agents code_supervisor --provider kimi_cli
```

The dialect is detected automatically; no configuration selects it.

## Dialect Detection

The provider probes the resolved `kimi` binary's `--help` **once per binary
identity** and classifies it from its capability flags:

| Dialect | Capability flags (any one) |
|---|---|
| Legacy | `--mcp-config`, `--mcp-config-file` |
| Kimi Code | `--auto`, `--agent-file`, `--output-format` |

Rules that matter:

- **Never a version string.** A version string is a claim; an option table is a
  fact. Kimi Code and `kimi-cli` share the `kimi` command name and have shipped
  under overlapping version numbers.
- **Whole-token matching.** `--agent` must not match `--agent-file`.
- **Fail closed.** An unrecognised option table yields `UNKNOWN`, and the
  provider refuses to launch rather than guessing.
- **Successful probes only are cached**, keyed on
  `(absolute path, st_mtime_ns, st_size)`. An `UNKNOWN` verdict must not become
  sticky — a transient PATH problem at boot would otherwise disable the
  provider for the life of the process.
- The probe and the launch run **in the same shell**, and the probe's absolute
  binary path is reused verbatim for `exec`. Probing one PATH while tmux
  launches another is the PR #664 bug class.

## Kimi Code Path

### Working Directory

The terminal's own working directory *is* Kimi's cwd. The provider deliberately
emits **no `cd`**: git-root resolution, repository `AGENTS.md` discovery,
project MCP discovery and the session namespace all depend on it. The legacy
temp-cwd workaround exists to dodge a per-directory lock that Kimi Code does
not have.

### Per-Worker `KIMI_CODE_HOME`

`KIMI_CODE_HOME` (default `~/.kimi-code`) is the **only** redirect Kimi Code
exposes for MCP membership — there is no launch-scoped MCP flag and no
MCP-config env override. Per-worker MCP isolation therefore means per-worker
home:

```
worker A -> <provider_temp>/kimi-home A -> mcp.json A
worker B -> <provider_temp>/kimi-home B -> mcp.json B
```

A single shared home is **not** a valid substitute: two CAO profiles may declare
different `mcpServers` surfaces (worker A: `cao` + `github`; worker B: `cao` +
`postgres`), and one `mcp.json` cannot represent both.

The runtime home is built from the *effective source home* — resolved from the
same launch shell that resolved the `kimi` binary, never from `cao-server`'s own
environment, which can differ.

**Copied** (allowlist, never a denylist, so a future Kimi release that invents a
new runtime-state directory cannot leak it into every worker):

| Kind | Entries |
|---|---|
| Files | `config.toml`, `tui.toml`, `AGENTS.md`, `mcp.json` |
| Directories | `skills/`, `plugins/`, `credentials/` |
| Snapshot | `workspace-trust/` (see below — security input, not ordinary state) |

`config.toml` and `mcp.json` are written `0600`; `credentials/` is copied `0600`
per file; the home itself is `0700`.

**Not copied.** Session, log, update, cache and telemetry state stay behind, so N
workers cannot corrupt each other's sessions or share a conversation index.
`iter_forbidden_runtime_state()` exists so tests can prove this.

**`workspace-trust/` is snapshotted** (A4). It is neither ordinary runtime state
nor ordinary user semantics: it records decisions the operator already made in
normal Kimi, and Kimi consults it to decide whether to raise the trust dialog at
all. Leaving it behind re-asked a question the operator had already answered, on
every terminal, which left the server-wide opt-in as the only unattended path.

The store is copied as **opaque bytes** — CAO never parses, keys, or regenerates
the record schema, because only Kimi decides what "trusted" means. The runtime
copy is a real directory of real files, so records Kimi writes during a worker's
life land only in the disposable home. Copy policy:

| Source shape | Behaviour |
|---|---|
| real directory | recursive snapshot; dirs `0700`, records `0600` |
| top-level symlink | **refused** — nothing inherited, warning logged |
| not a directory | refused, warning logged |
| symlink inside the store | that entry is skipped (never materialised, never followed) |

The refusals are deliberate: following a symlinked trust store would adopt
whatever store it points at, which may not be the user's, and a retained link
inside the snapshot is a path back out of it. A refused store simply means the
worker falls through to the normal trust policy below.

**`bin/` is referenced, not copied** — it holds Kimi's self-managed ~175 MB
binary, which every CAO-managed worker runs with auto-update disabled. Known,
accepted risk: it is the one entry that can still reach back into the real home,
and if the source `bin/` is itself a symlink the runtime home gets a
link-to-a-link.

**Symlinked preserve-entries.** If `skills/`, `plugins/` or `credentials/` is a
top-level symlink, the target's *contents* are copied and the link is never
reproduced, so nothing can be written back through it. A link whose target is
not a directory, or which points at the source home or any of its ancestors, is
refused, logged, and recorded in `skipped_dirs` — the home still builds, so a
pathological link degrades one entry rather than blocking the launch. Links
*inside* a preserved tree are copied as links and never walked through.
`workspace-trust/` deliberately does **not** follow that rule — see above.

`cleanup()` removes exactly `<provider_temp>/kimi-home`, and unlinks a
symlinked home rather than following it.

### Workspace Trust — inherit one, or opt in to all

Kimi Code refuses to run in an untrusted folder. Measured against 0.43.1:

| Selection | Result |
|---|---|
| **Don't trust** | **the process exits immediately** (exit 0). No TUI, no turn. |
| **Trust this folder** | full function; trust recorded for that folder |

Because the dialog fires for **any** untrusted cwd — even a repository with no
project MCP files — and because CAO builds a fresh `KIMI_CODE_HOME` per
terminal, trust is not optional for a working worker.

But *granting* trust is a security decision, because trusting a folder:

- **starts that repository's project MCP servers** — arbitrary commands read
  straight out of the checkout (both `.mcp.json` and `.kimi-code/mcp.json` are
  discovered), and
- **loads that repository's project `AGENTS.md`**, i.e. instructions.

A worker launched in a folder the operator does not control would otherwise
execute that folder's commands with no human in the loop. **CAO does not grant
trust by default.** It refuses to answer the dialog and fails the terminal with
an actionable error naming the folder.

There are two supported ways to resolve it, and they are not equivalent.

#### Option A — pre-trust one workspace (preferred)

Run normal `kimi` once in that repository, using your usual `KIMI_CODE_HOME`,
and choose **Trust this folder**:

```bash
cd /path/to/repo && kimi      # then choose "Trust this folder"
```

Kimi records the decision in `$KIMI_CODE_HOME/workspace-trust/`. Every later CAO
terminal in that folder **inherits the record through the runtime-home
snapshot**, so Kimi sees the folder as already trusted and raises no dialog —
CAO never has to decide anything. This is the narrow choice: it trusts exactly
the repositories you reviewed.

#### Option B — server-level auto-trust (broader)

Set in the **cao-server** environment:

```bash
CAO_KIMI_CODE_TRUST_WORKSPACE=1
```

This permits CAO to answer the dialog itself, granting trust to **whatever
folder a terminal is launched in** — including folders you have never reviewed.
That is strictly broader than Option A, so it is not the recommended default;
prefer Option A unless you routinely launch terminals in folders you have not
pre-trusted.

Accepted truthy values are `1`, `true`, `yes`, `on` (case-insensitive,
whitespace-trimmed). Every other value — including unset, `0`, `false` and a
typo — means "do not grant", so a mistake fails closed.

The variable is CAO's own policy knob and is **not** exported to Kimi: 0.43.1
exposes no `--trust` flag, and its trust record
(`$KIMI_CODE_HOME/workspace-trust/wd_<name>_<sha256[:12]>`, containing
`{"root": …, "trustedAt": …}`) is an undocumented on-disk format that CAO treats
as opaque.

#### What happens in each case

| Source home says | Opt-in | Outcome |
|---|---|---|
| folder already trusted | unset | no dialog; Kimi runs; CAO decides nothing |
| folder not trusted | unset | dialog; CAO sends no key; terminal fails with the error below |
| folder not trusted | `=1` | dialog; CAO identifies it positively and accepts |
| trust store is a symlink | either | nothing inherited; falls through to the rows above |

When opted in, the handler is still **positive-identification only**: the exact
dialog must be present (title + navigation hint + a recognised option set), the
workspace the dialog names must equal the pane's actual working directory, and
the selection must be readable. If the selection is already *Trust this folder*
it is accepted; otherwise CAO navigates deterministically, re-reads the pane,
verifies the marker moved, and only then sends Enter. Any other outcome —
unknown layout, unreadable selection, mismatched workspace — fails closed
without sending a key. A blind Enter is never sent.

In either case the trust record Kimi writes during the run lands **only in the
disposable runtime home**. The real `$KIMI_CODE_HOME/workspace-trust/` is never
written by a CAO worker, so Option B stays a per-launch behaviour rather than a
silent mutation of your persistent trust store.

### MCP Server Configuration

There is no launch-scoped MCP flag in Kimi Code. Profile MCP servers are merged
into the runtime home's `mcp.json`:

```json
{
  "mcpServers": {
    "cao-mcp-server": { "command": "…", "args": ["…"] }
  }
}
```

Merge semantics are frozen and deterministic:

- **A profile server overrides a user-level server of the same name.** The
  profile is the explicitly selected, launch-specific configuration; the user
  file is ambient.
- Every profile entry passes through `resolve_mcp_server_config`, so a bundled
  `cao-mcp-server` becomes a PATH-independent invocation exactly as on the
  legacy path.
- `CAO_TERMINAL_ID` is **not** injected per server. Kimi Code's stdio MCP
  children inherit the parent environment, so the launch line exports it once
  for every server, including user-level ones. An explicit `env` block on a
  profile server is preserved verbatim, including an explicit `CAO_TERMINAL_ID`.

### MCP Timeouts — milliseconds

Kimi Code binds `[mcp] startup_timeout_ms` / `[mcp] tool_timeout_ms` from
`config.toml` **or** these launch-scoped env vars. Per-server
`startupTimeoutMs` / `toolTimeoutMs` override them.

CAO sets, on the launch line:

| Binding | Value | Meaning |
|---|---|---|
| `KIMI_MCP_STARTUP_TIMEOUT_MS` | `60000` | 60 s — 2× Kimi's own 30 s default, enough for a cold `cao-mcp-server` import |
| `KIMI_MCP_TOOL_TIMEOUT_MS` | `600000` | 600 s — CAO's legacy handoff budget, preserved |

Both are **milliseconds**, verified against Kimi Code 0.43.1's bundled source
rather than assumed: the env bindings are read through a parser whose body is
`Number(raw)`, accepted only when
`Number.isInteger(parsed) && parsed >= 1 && parsed <= 2147483647`, and the
schema is `McpTimeoutMsSchema = number().int().min(1).max(2147483647)`.

A profile's single `timeout` field is mapped onto both per-server fields and
clamped to `1 … 2147483647`; the `timeout` key itself is dropped, since it is
not part of Kimi's server schema. An out-of-range env value is silently ignored
by Kimi and falls back to the config default — which is why CAO always sets
both explicitly.

### Agent Profiles

Kimi Code takes a **Markdown** agent file via `--agent-file`, not YAML. CAO
writes a launch-scoped file next to the runtime home:

```markdown
---
name: cao-kimi-<terminal-id>
description: "CAO launch-scoped agent for terminal <terminal-id>"
---

${base_prompt}

<CAO's system prompt>
```

`${base_prompt}` is emitted **before** CAO's own text and is never interpolated
by CAO. It is Kimi Code's own substitution point for the built-in base prompt,
so emitting it first *appends* CAO's instructions to Kimi's base prompt instead
of replacing the whole prompt. Substituting it in CAO would silently drop
Kimi's own instructions — the PR #664 failure class.

The frontmatter `name` must be non-empty kebab-case, which a terminal id is not;
the provider slugifies it to `cao-kimi-<slug>`.

## Legacy Path

Everything in this section applies only when detection resolves to the legacy
`kimi-cli` TUI.

### Status Detection

| Status | Pattern | Description |
|--------|---------|-------------|
| **IDLE** | `💫` or `✨` at bottom (optionally prefixed with `username@dirname`) | Prompt visible, ready for input |
| **PROCESSING** | No prompt at bottom | Response is streaming |
| **COMPLETED** | Prompt at bottom + latching flag (user input detected) | Task finished |
| **ERROR** | `Error:`, `APIError:`, `ConnectionError:` patterns | Error detected |

#### Prompt Symbols

- **💫** (dizzy): Thinking mode enabled (default behavior)
- **✨** (sparkle): Thinking mode disabled (`--no-thinking` flag)

The provider matches both symbols using the pattern `(?:\w+@[\w.-]+)?[✨💫]`. The
`username@dirname` prefix is optional to support both v1.20.0+ (bare emoji) and
earlier versions.

### Message Extraction

Response extraction from terminal output (supports two formats):

**v1.20.0+ (inline prompt format):**
1. Find the last prompt-with-input line (`💫 message text`)
2. Collect all content between that line and the next bare prompt (`💫`)
3. Filter out thinking bullets (gray ANSI-styled `•` lines)

**Pre-v1.20.0 (input box format):**
1. Find the last user input box (bordered with `╭─` / `╰─`)
2. Collect all content between the box end and the next prompt
3. Filter out thinking bullets

**Fallback** (long responses where markers scroll out of capture): Extract all
content up to the last idle prompt, filtering out TUI chrome.

### Thinking vs Response Bullets

Both thinking and response lines use the `•` (bullet) prefix. The provider
distinguishes them using ANSI color codes in the raw terminal output:

- **Thinking**: `\x1b[38;5;244m•` (gray color 244 + italic)
- **Response**: plain `•` without an ANSI color prefix

Kimi Code uses `●` in colour 253 for the answer. A 24-bit (`38;2;R;G;B`) bullet
counts as reasoning only when its channels are near-grey (spread ≤ 16); the
measured answer colour 253 is decisive in the other direction. The asymmetry is
deliberate: both of Kimi's own styles are greyscale, so a truecolor row errs
toward *reasoning*, and a misread answer raises an extraction error (loud)
rather than a misread reasoning row leaking silently into the message.

### Agent Profiles

Legacy profiles are **optional**. If provided, the provider:

1. Creates a temporary YAML agent file that extends Kimi's built-in `default`
   agent
2. Writes the system prompt as a separate markdown file
3. Passes the agent file via `--agent-file`

```yaml
version: 1
agent:
  extend: default
  system_prompt_path: ./system.md
```

Temp files are automatically cleaned up when the provider's `cleanup()` method
is called.

### MCP Server Configuration

MCP servers from agent profiles are passed via `--mcp-config` as a JSON string:

```bash
kimi --yolo --mcp-config '{"server-name": {"command": "npx", "args": ["-y", "cao-mcp-server"]}}'
```

#### Transport selection

Kimi CLI hands each `--mcp-config` document to FastMCP. FastMCP's remote server
model has no `type` field, and when `transport` is absent it infers one from the
URL *path* — `sse` when the path ends in `/sse`, Streamable HTTP otherwise. A
declared SSE server published at `/events` would therefore start as Streamable
HTTP, and a Streamable HTTP server published at `/sse` would start as SSE.

CAO writes FastMCP's `transport` explicitly from an [agent plugin](agent-plugins.md)
server's portable `type`, so the declared protocol is selected rather than guessed
from URL spelling. A `cwd` is passed through unchanged and is honoured by FastMCP
for stdio servers. An entry that carries no `type` — a hand-written profile entry —
is left exactly as written, so FastMCP's own inference still applies to it.

#### MCP Tool Call Timeout

Kimi CLI defaults to a 60-second MCP tool call timeout
(`tool_call_timeout_ms=60000` in `~/.kimi/config.toml`). This is too short for
`handoff` operations, which create a worker terminal, wait for completion, and
extract output — routinely exceeding 60 seconds.

The provider automatically modifies `~/.kimi/config.toml` to set
`tool_call_timeout_ms=600000` when MCP servers are configured, increasing the
timeout to 600 seconds (10 minutes) to match CAO's default handoff timeout. The
original value is restored during `cleanup()`. This is the same
direct-config-write pattern used by the Antigravity CLI provider
(`~/.gemini/config/mcp_config.json`).

**Why not `--config` flag?** Kimi CLI's `--config` flag causes it to bypass the
default config file (`~/.kimi/config.toml`), which breaks OAuth authentication —
the CLI shows "model: not set" and `/login` refuses to work. Modifying the
config file directly avoids this issue.

Without this override, the supervisor Kimi CLI agent receives a
`ToolError("Timeout while calling MCP tool handoff")` after 60 seconds, even
though the worker is still processing.

Note the contrast with Kimi Code, which needs no config-file mutation at all —
its timeouts travel as launch-scoped env bindings.

#### CAO_TERMINAL_ID Forwarding

Kimi CLI does not automatically forward parent shell environment variables to
MCP subprocesses. The provider explicitly injects `CAO_TERMINAL_ID` into the
`env` field of each MCP server config so that tools like `handoff` and `assign`
can create new agent windows in the same tmux session (instead of creating
separate sessions). Existing `env` entries are preserved, and an existing
`CAO_TERMINAL_ID` value is never overwritten.

### Command Flags

| Flag | Purpose |
|------|---------|
| `--yolo` | Auto-approve all tool action confirmations |
| `--agent-file FILE` | Custom agent YAML file |
| `--mcp-config TEXT` | MCP server configuration (JSON, repeatable) |
| `--work-dir DIR` | Set working directory |
| `--no-thinking` | Disable thinking mode (changes prompt to ✨) |

## Implementation Notes

### Provider Lifecycle

**Legacy:**

1. **Initialize**: Create unique temp dir → set MCP timeout in
   `~/.kimi/config.toml` (if MCP servers) → wait for shell → send
   `cd <tempdir> && TERM=xterm-256color kimi --yolo` → wait for IDLE or
   COMPLETED (up to 120s)
2. **Status Detection**: Check bottom 50 lines for idle prompt pattern
   (end-of-line anchored)
3. **Message Extraction**: Line-based approach mapping raw and clean output for
   thinking filtering
4. **Exit**: Send `/exit` command
5. **Cleanup**: Remove temp agent files, restore MCP timeout in `config.toml`,
   reset state

**Kimi Code:**

1. **Initialize**: probe the launch shell for the binary + dialect + source
   `KIMI_CODE_HOME` → build the runtime home and merge MCP servers → launch
   `env KIMI_CODE_HOME=… CAO_TERMINAL_ID=… TERM=xterm-256color
   KIMI_MCP_*_TIMEOUT_MS=… KIMI_*_NO_AUTO_UPDATE=1 <binary> --auto [--model M]
   [--agent-file P]` in the terminal's real cwd → answer the workspace-trust
   dialog (opt-in only) and the upgrade reminder → wait for IDLE or COMPLETED
2. **Status Detection**: buffer path and rendered-screen path, both sharing one
   classifier
3. **Message Extraction**: region located by layout (the composer sits *below*
   the transcript), rows classified by the shared classifier
4. **Cleanup**: remove the temp dir and the runtime home

### Status Detection Details (both dialects)

Readiness on the Kimi Code TUI is the status bar / context footer. A turn is
in flight when a **braille** indicator is present; under Kimi Code semantics a
moon is not evidence of work, because that dialect rotates moons through its
*idle* tip row. The legacy bare-moon signal is preserved unchanged.

Two independent paths exist and must agree:

- `get_status(output)` — the raw rolling buffer. It strips pipe-pane escapes
  first, so the bottom-anchored checks see line-oriented text.
- `get_status_from_screen(screen_lines)` — a pyte-composited viewport of
  **escape-free** rows. Enabled via `supports_screen_detection = True` /
  `CAO_PYTE_STATUS`. Because the rows carry no styling, no rule here may depend
  on ANSI.

A response bullet latches "input received", so a long response that scrolls its
bullets out of the buffer still reads COMPLETED rather than IDLE. Nothing
latches at init, so a freshly-launched terminal reads IDLE. A bullet only
counts when it carries a **payload**: on a narrow terminal the status bar wraps
and a row can begin with a bare `●` or `•` followed by punctuation (`●)`).
Matching that as assistant output latched a terminal that had never been sent
anything and reported it COMPLETED. Both dialects therefore share one bullet
rule, `BULLET_ANY_RE = ^[^\S\n]*[•●][^\S\n]+`.

Chrome classification is **structural**, never substring-based. A row is boot
chrome or footer chrome because of its measured shape — whole-row anchoring, the
frame colour, the segment count — not because it mentions boot vocabulary. This
matters because `_locate_response_region()` uses those rows as end anchors, so a
substring rule truncates any answer that quotes them:

```
● First line
connecting to mcp servers is only a phrase   <- must survive
Final line                                   <- must survive
```

### Terminal Output Format (v1.20.0+, legacy)

```
╭────────────────────────────────────────────────────────╮
│ Welcome to Kimi Code CLI!                              │
╰────────────────────────────────────────────────────────╯
💫 create a function
• [thinking] Let me create the function...
• Here is the function:

def greet(name):
    return f"Hello, {name}!"

💫
```

### Kimi CLI v1.20.0 Compatibility

The provider handles several v1.20.0 behavioral changes:

- **Prompt format**: Changed from `user@dirname💫` to bare `💫`. The idle
  pattern uses an optional prefix.
- **Input display**: Removed bordered input boxes (`╭─...╰─`). User input now
  appears inline on the prompt line (`💫 message text`).
- **TERM variable**: Kimi CLI silently exits when `TERM=tmux-256color` (the tmux
  default). The provider overrides with `TERM=xterm-256color`.
- **Per-directory lock**: Only one Kimi instance can run in a given directory.
  Each provider instance uses its own temp directory via `cd`.

## E2E Testing

```bash
# Run all Kimi CLI E2E tests
uv run pytest -m e2e test/e2e/ -v -k kimi_cli

# Run specific test type
uv run pytest -m e2e test/e2e/test_handoff.py -v -k kimi_cli
uv run pytest -m e2e test/e2e/test_assign.py -v -k kimi_cli
uv run pytest -m e2e test/e2e/test_send_message.py -v -k kimi_cli
uv run pytest -m e2e test/e2e/test_supervisor_orchestration.py -v -k KimiCli -o "addopts="
```

Prerequisites for E2E tests:
- CAO server running (`cao-server`)
- `kimi` CLI authenticated (`kimi login`)
- Agent profiles installed (`cao install developer`)
- For Kimi Code: pre-trust the test repo in normal Kimi (Option A, preferred), or
  set `CAO_KIMI_CODE_TRUST_WORKSPACE=1` in the server environment (Option B) — see
  *Workspace Trust* above

## Troubleshooting

### Kimi CLI not detected

```bash
# Verify kimi is on PATH (command is `kimi`, not `kimi-cli`)
which kimi
kimi --version
```

### Dialect resolves to UNKNOWN

The provider refuses to launch rather than guess. Confirm the binary's option
table is intact:

```bash
kimi --help    # expect --auto / --agent-file, or --mcp-config
```

An `UNKNOWN` verdict is never cached, so fixing the binary is enough — no
restart of `cao-server` is required for the retry.

### "Kimi Code is asking to trust this folder and CAO will not answer it"

Working as intended. The folder is not trusted in your real `KIMI_CODE_HOME`.
Either pre-trust that one repository in normal Kimi (Option A, preferred — the
decision is inherited and the prompt does not come back), or set
`CAO_KIMI_CODE_TRUST_WORKSPACE=1` in the cao-server environment and restart it
(Option B — broader, trusts whatever folder a terminal launches in). See
*Workspace Trust* for why neither is automatic.

If you *did* pre-trust the folder and still see this, check that the trust store
was inheritable: a `workspace-trust` that is itself a symlink is refused by
design, and the provider log will contain
`kimi_runtime_home_trust_skip reason=top-level-symlink`.

### Trust dialog answered but the terminal still times out

The handler fails closed on a workspace mismatch: the folder Kimi names must
equal the pane's actual working directory. Check the provider log for
`kimi_trust_pane_cwd_unavailable` or the workspace-mismatch message.

### Authentication issues

```bash
# Re-authenticate
kimi login
```

### Initialization timeout

If Kimi CLI takes too long to start, check:
- Network connectivity (Kimi requires API access)
- Authentication status (`kimi login`)
- The provider waits up to 120 seconds for initialization
- For Kimi Code, whether a project MCP server is hanging startup — bound by
  `KIMI_MCP_STARTUP_TIMEOUT_MS` (60 s)

### Status bar not detected

The provider checks the bottom 50 lines for the idle prompt
(`IDLE_PROMPT_TAIL_LINES = 50`). This accounts for Kimi's TUI padding lines
between the prompt and the status bar, which varies with terminal height (e.g.,
a 46-row terminal has ~32 empty padding lines). If Kimi's TUI layout changes
significantly, this constant may need adjustment.

### A finished answer is extracted as empty or raises an extraction error

Extraction raises `OutputExtractionError` rather than returning something
plausible but wrong. Two common causes:

- every candidate row looked like reasoning — CAO refuses to return private
  reasoning as the agent's message;
- the answer quoted TUI chrome and a region anchor landed inside it. This is
  covered by regression tests; if it recurs, the row shape has changed and the
  shared classifier in `providers/kimi_transcript.py` needs the new measurement.
