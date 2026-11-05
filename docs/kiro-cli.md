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
an agent-plugin MCP refresh, keep whichever format is on disk. A file that
Kiro's own upgrader rewrote (it has a `kiroUpgrade` key) counts as v2 here, so
it gets the v2 file again, and a bounded launch compares it with the v2 tool
names.

### What CAO checks

Before anything is allocated:

- the kiro-cli version is one CAO supports (see below);
- the agent is not unrestricted (`*`);
- the profile's `kiroConfig.effort`, if set, is a plain level such as `high`;
- no `KIRO_HOME` other than `~/.kiro` reaches the launch;
- `cao install` writes Kiro agents to `~/.kiro/agents`, the only place v3
  loads them from. With `CAO_AGENTS_DIR` set to another directory, the file CAO
  checks would not be the file Kiro runs, so a v3 launch is refused
  (`kiro_v3_agents_dir_unsupported`). This check is stricter than v2's, which
  still allows `CAO_AGENTS_DIR`;
- the installed agent file on disk is exactly what `cao install --engine v3`
  writes for its grant, and that grant fits the launch;
- the launch directory and its parents have no `.kiro/` configuration v3 would
  load (see *Workspace trust*).

After kiro-cli starts, and before any task is typed:

- no dialog is on screen (CAO never answers the workspace-trust dialog);
- the header names the agent (as on v2);
- the workspace check runs again;
- CAO reads the agent file's grant again (it must still fit the launch), then
  types `/mcp`: exactly the configured MCP servers must be running, each with a
  tool. While a server is still loading, CAO closes `/mcp` and opens it again,
  until `provider_init_timeout` has passed (see *How long CAO waits for MCP
  servers*).
  Only then does it type `/tools`, so a server's tools are on that panel when it
  is read: every tool tag must be one that the grant's tools carry, and every
  MCP tool must be one the grant names (`@server`, or `@server/tool` for a
  single tool). The tags come from the pinned engine schema: `fs_write` gives
  the `code` tool, which carries `read` as well as `write`, so a write-only
  grant shows both. A list too long for the pane is read screen by screen (see
  *How CAO reads a long tool list*).

Only then does the terminal accept input. If any check fails, no task is typed
and the window is closed.

The launch command is:

```bash
/usr/bin/env -u ASBX_KIRO_MANDATORY_MCPS -u KIRO_AGENT_CONFIG_DIR -u KIRO_AGENT_ENGINE -u KIRO_HOME \
  kiro-cli chat --agent-engine v3 --trust-workspace --trust-all-tools [--model m] [--effort e] --agent <name>
```

`--effort` comes from the profile's `kiroConfig.effort`, when set.

### How long CAO waits for MCP servers

**Why it matters.** An MCP server such as `cao-mcp-server` needs a few seconds
to start after kiro-cli does. CAO types no task until every configured server
is running: before that, `/tools` doesn't list the server's tools, so CAO
couldn't check them.

**What CAO does.** Kiro v3's `/mcp` panel shows the servers as they were when
the panel opened, and it never changes while it stays open. Reading an open
panel again would never show a server that has since started. So CAO:

1. opens `/mcp` and reads it;
2. if a server is still loading, closes the panel (Escape) and waits for the
   prompt to come back;
3. waits 1 s, then 2 s, then 3 s before each later try, and opens `/mcp` again;
4. stops as soon as every server is running, and goes on to `/tools`.

CAO checks the time after each open. If a server is still loading at the first
check after `provider_init_timeout` has passed since the first open (60 s by
default), the launch is refused. On that refusal path the open's close/prompt
wait does not run (CAO sends a bare Escape and raises). The time after the
timeout is this open's palette and panel reads, plus the close of the previous
open if that close was still running when the timeout passed. So the refusal
comes at most about 7.5 s after the timeout. On a pass, `/tools` is read next,
with its own bound (see *How CAO reads a long tool list*). The message says how
long CAO waited and how often it opened `/mcp`, for example:

```text
kiro_v3_mcp_not_ready: MCP servers still loading after 61.5 s (13 opens of /mcp): ['cao-mcp-server']
```

A failed, extra or missing server refuses at once, without another try. So does
a dialog that appears after `/mcp` closes: CAO never answers it, and types
nothing more.

**To wait longer** for a slow server, raise `provider_init_timeout` (in
seconds) where `cao-server` runs, with `CAO_PROVIDER_INIT_TIMEOUT=120`, or in
`settings.json`:

```json
{ "server": { "provider_init_timeout": 120 } }
```

The same setting is also how long CAO waits for the agent prompt at startup
(see [Configuration](configuration.md)).

**To see how long a server took to start**, run `cao-server` with
`CAO_LOG_LEVEL=DEBUG`. CAO then logs each open of `/mcp` with the time since the
first open and each server's status. At the default level, `INFO`, these lines
are left out.

### How CAO reads a long tool list

**Why it matters.** CAO must see every row of `/tools` before it types a task:
a row it didn't see could be a tool the grant doesn't allow. But a long list
doesn't fit on one screen. Kiro shows as many rows as fit, which is the pane's
height minus nine (at least five), and draws `↓ more` below them. A helper with
just `fs_read` and `@cao-mcp-server` already has 36 tags: `read`, plus the 35
tools of `cao-mcp-server`.

**Where the panel's top goes.** Kiro draws the panel in place of the input box,
under the session's notices and with three blank rows kept below it. So once
the list scrolls, the panel is always two or three rows taller than the pane,
and its top rows, the title among them, go above the pane's top row into its
scrollback. A list that doesn't scroll can overflow too: on CAO's 50-row tmux
panes, more than about 40 tags do. So every `/tools` read is the pane plus its
recent scrollback:
- **tmux:** the visible pane and the 100 lines above it (`capture-pane -S -100`);
- **herdr:** the pane's last 100 lines (`pane read --source recent --lines 100`).
  A scrolling panel in a herdr pane taller than about 97 rows doesn't fit in
  that read, and refuses.

Each read must hold exactly one `/tools` title, and nothing but blank rows may
follow the panel's footer, so the panel CAO reads is the last thing on the pane.

**Old rows above the pane.** By default Kiro keeps the scrollback (its
`chat.preserveScrollback` setting is on, and CAO leaves it alone). When a frame
changes, Kiro repaints only the pane's own rows, so rows already above the pane
keep the text they had when they left it.
- **On a pane of 12 rows or more,** those rows are only the notices, the title,
  its divider and the search row, which don't change while the list scrolls. So
  the read is the whole current panel.
- **Below 12 rows,** the list's own rows go above the pane too, and after the
  first Down the read is old rows under the one title. CAO refuses it.

What keeps old rows out is the check that each screen is the last one moved by
exactly one row: the "one title" rule only catches copies of the panel, as a
terminal that kept every frame's overflow would leave.

**What CAO does.**

1. It opens `/tools` and reads the first screen.
2. While that screen ends in `↓ more`, it presses Down once, which moves Kiro's
   list by one row, and reads the screen again. The new screen must be the old
   one moved by exactly one row: the same title, the same number of rows, the
   old rows without the top one, and one new row at the bottom. A screen that
   hasn't redrawn yet is read again, every 0.5 s for up to about 2.5 s.
3. At the screen with no `↓ more` left, it joins the rows and presses Escape,
   then waits for the empty prompt to come back on the pane, as after `/mcp`.
4. It checks the joined list just as it checks a list that fits: no row twice,
   exactly as many rows as the title declares (`/tools · 36 tags`), and every
   tag and MCP tool allowed by the grant.

For example, on a 30-row pane Kiro shows 21 of the 36 tags, so CAO presses Down
15 times; on a 24-row pane, 21 times. This is the same on tmux and herdr. On a
pane under 12 rows it refuses after one Down (see above).

Down is the only key CAO sends into the panel. It sends it only when the screen
it has just read shows the panel with `↓ more` and no dialog. CAO never types
into the panel's search box. If a dialog appears, CAO refuses without pressing
anything, not even Escape.

**How long it may take.** The read is bounded by `provider_init_timeout` (60 s
by default; see *How long CAO waits for MCP servers*), counted from the first
`/tools` key and checked before each Down, so a refusal on time comes at most
one screen's wait (about 2.5 s) after it. The read and CAO's final wait for the
prompt share that one timeout: a long scroll shortens the final wait, down to
about 2.5 s, instead of adding a timeout of its own.

**When it refuses.** Each of these starts with `kiro_v3_tools_mismatch`, and no
task is typed:

| The message ends with | What happened | Fix |
|---|---|---|
| `the /tools panel did not move down by one row` | a screen skipped a row, didn't redraw within the wait, or showed a change in its rows or its count while CAO read it; or the pane is under 12 rows tall, so the read held old rows above the pane | make the pane at least 12 rows tall; otherwise retry, and if it persists, report it |
| `the panel declares N tags but M are visible` | some rows the title counts were never shown | report it |
| `the /tools panel shows [...] more than once` | the same row appeared twice | report it |
| `the /tools panel shows more rows than the N it declares` | the list went on past the count in its title | report it |
| `the /tools panel was still scrolling after N s (M of N tags read)` | `provider_init_timeout` passed before the last screen | make the pane taller, or raise `provider_init_timeout` |
| `no complete /tools panel on screen` | among other causes: the panel's title wasn't in the read (a herdr pane over about 97 rows), or the read held two titles (old frames left in the scrollback) | make the herdr pane shorter; if it persists, report it |
| `something is drawn below the /tools panel` | the panel wasn't the last thing on the pane | report it, with a screenshot |
| `a dialog appeared on /tools; it was not answered and nothing more was typed` | a dialog came up while CAO read the list | report it, with a screenshot |
| `the prompt did not return after /tools` | after Escape, the empty prompt didn't come back within about 2.5 s | retry; if it persists, report it |
| `a dialog appeared after /tools closed; it was not answered and nothing more was typed` | a dialog came up instead of the prompt | report it, with a screenshot |

A change that keeps the count and touches only rows CAO has already scrolled
past isn't seen, just as a change made right after a list that fits has been
read isn't.

**Not yet seen live.** This follows the code of Kiro's own terminal UI in
kiro-cli 3.0.0, which is the same in 2.37.0. That covers the `↑ more` and
`↓ more` rows, the one-row Down, the title that counts every row, the three
rows kept below the panel, and how an overflowing frame is redrawn: by default
Kiro repaints only the pane and keeps the rows above it as they were (with
`chat.preserveScrollback` off it clears the scrollback and writes the whole
frame again instead). A live helper run's herdr history did show the top of an
open panel, title included, above the pane. But no scrolled `/tools` panel has
been read end to end on a live run yet, on either backend. If Kiro draws it
differently, CAO refuses the launch: it never passes a list it didn't read in
full. With Kiro's icons turned off (`v more`), or in its screen-reader layout,
the panel is refused too.

### Supported kiro-cli versions

| kiro-cli | v3 engine | Status |
|---|---|---|
| 2.35.1 | `@kiro/agent` 0.66.70 | measured |
| 2.36.0 | `@kiro/agent` 0.66.70 | pending operator verification: refused at launch unless the operator opts in (below) |
| 2.37.0 | `@kiro/agent` 0.66.70 | pending operator verification: refused at launch unless the operator opts in (below) |
| 3.0.0 | `@kiro/agent` 0.66.70 | pending operator verification: refused at launch unless the operator opts in (below) |

Any other version refuses v3; v2 is not affected. A new kiro-cli release needs a
CAO update before v3 runs on it.

Every pending version runs the same v3 engine as 2.35.1, so CAO checks it with the
same rules. Each was added by reading its installed engine and help screens, without
running a chat. What a pending version still lacks is a live run of the operator
checklist.

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
| `kiro_v3_agents_dir_unsupported` | `CAO_AGENTS_DIR` makes `cao install` write Kiro agents somewhere other than `~/.kiro/agents`, which is where v3 loads them from | unset `CAO_AGENTS_DIR` for `cao-server` and `cao install`, then `cao install <name> --provider kiro_cli --engine v3`; or `--engine v2` |
| `kiro_v3_not_installed` | no agent file for this agent | `cao install <name> --provider kiro_cli --engine v3` |
| `kiro_v3_agent_unreadable` | something that can't be read as a file is where the agent file goes (a directory, a symlink loop, no permission) | remove it, then `cao install <name> --provider kiro_cli --engine v3` |
| `kiro_v3_bad_effort` | the profile's `kiroConfig.effort` is not a plain level such as `low`, `medium` or `high` | fix `kiroConfig.effort`, or `--engine v2` |
| `kiro_v3_bad_cwd` | CAO could not read the launch directory to check it, or the pane's own directory at the re-check after startup | launch from an existing directory |
| `kiro_v3_invalid:<check>` | the agent file on disk is not CAO's v3 render (a v2 file, a hand edit, or Kiro's own upgrader rewrote it) | `cao install <name> --provider kiro_cli --engine v3`; the message names any `.bak` copy Kiro left |
| `kiro_v3_workspace_config:<entry>` | the workspace's `.kiro/` (or a parent's) holds configuration v3 would load, an entry CAO does not know, a symlink, or can't be read | remove it, use a worktree without it, or `--engine v2` |
| `kiro_v3_trust_dialog` | kiro-cli asked to trust the workspace anyway, including when that dialog was declined and Kiro exited | report it; CAO never answers that dialog |
| `kiro_v3_startup_dialog` | kiro-cli showed a dialog or a selector at startup that CAO doesn't answer: one it has no pattern for, the approval picker, or v2's "Switch to V3" offer | report it with a screenshot |
| `kiro_v3_tools_mismatch` | `/tools` shows a tag or MCP tool the grant does not allow, shows no tools, or shows a layout CAO can't read (a clipped list too); a list too long for the pane could not be read in full (see *How CAO reads a long tool list*); the `/tools` palette didn't appear; the launch allows every tool; or the agent file is no longer a v3 universal file, or its grant no longer fits the launch | reinstall; if it persists, report it |
| `kiro_v3_mcp_not_ready` | `/mcp` shows an extra, failed or missing server, one still loading once `provider_init_timeout` has passed (see *How long CAO waits for MCP servers*), or a layout CAO can't read; the `/mcp` palette didn't appear; a dialog appeared after `/mcp` closed; or the prompt didn't come back after a panel | check that the server starts on its own; for a slow server, raise `provider_init_timeout`; then retry |
| `kiro_v3_mcp_not_ready` (ungranted servers) | the agent file configures an MCP server its grant doesn't name | `cao install <name> --provider kiro_cli --engine v3` |

### Known limitations

With `provider_init_timeout` above about 90 s, on a heavily loaded machine, a
successful v3 start can take longer than the client waits for it: `cao launch`
then reports a failure while the server goes on to start the agent. At the
default 60 s there are about two minutes to spare.

### FAQ

**Will my v2 agents change?** No. Only `cao install --engine v3`, or a profile
that says `engine: v3`, writes the universal file; every other install is
byte-identical to before.

**Can I use `hooks`, `toolsSettings` or `toolAliases` on v3?** Not yet: CAO
refuses to install such a profile for v3 rather than approximate it.

**Does CAO answer Kiro's approval prompt on v3?** No. It reads as waiting for an
answer, for you or a supervisor.

### Ephemeral helpers on v3

> **Status: preview, pending the operator checklist.** Like v3 itself, Kiro helpers are
> built and unit-tested, but CAO claims them only on a kiro-cli version whose Phase 2
> operator checks have passed.

**Why it matters.** An ephemeral helper is a short-lived agent another agent creates
with `create_ephemeral_agent` (see [Ephemeral agents](ephemeral-agents.md#kiro-helpers)).
A Kiro helper must get exactly the tools its creator granted and nothing from your own
Kiro setup: not your agents, settings, hooks or MCP servers, and not the powers or the
secret store Kiro would otherwise copy out of `~/.kiro`. So every helper runs on v3 from
a throwaway Kiro home CAO makes for that one launch and removes afterwards, and CAO
never changes anything in `~/.kiro`.

#### Quick start

```json
{ "ephemeral": { "enabled": true, "allowed_providers": ["claude_code", "kiro_cli"] } }
```

Add `kiro_cli` to `ephemeral.allowed_providers` in `settings.json` (it is never on by
default). A creator then asks for `"provider": "kiro_cli"` in its spec. No engine is
needed: helpers always run on v3. You need a kiro-cli version from the
[supported table](#supported-kiro-cli-versions) on the CAO server's `PATH`.

#### What CAO checks

Before anything is allocated:
- the engine is v3 (an explicit `v2` refuses), and the kiro-cli version is supported;
- Kiro would not copy anything from `~/.kiro` into the helper's home (CAO only checks
  whether files exist there; it never opens them);
- kiro-cli resolves to an absolute path, no `KIRO_HOME` is forwarded, and the CAO home
  is a path Kiro would accept as a `KIRO_HOME`;
- the launch directory passes the [workspace check](#workspace-trust), and is not your
  home folder (or inside a home folder that is a git repository).

Before the window opens, CAO builds the helper's home and checks both of its agent
files with the same validator `cao install --engine v3` uses. After kiro-cli starts, and
before any task, it checks the header, runs the workspace check again, confirms Kiro's
copies did not run (the placeholders and your `~/.kiro` markers are unchanged), and
holds `/tools` and `/mcp list` to the helper's own file.

#### When a helper is refused

The message never names a path on the server; the server log does. The table covers the
helper's own refusals. The [v3 table above](#when-a-v3-launch-is-refused) applies too,
except that a helper is never told to use engine `v2`: the version and wrapper rows below
replace those.

| Message starts with | What it means | Fix |
|---|---|---|
| `kiro_ephemeral_engine:v2` | the request asked for engine `v2`; helpers run on v3 only | omit the engine, or pass `v3` |
| `kiro_v3_global_home:secrets` | you have a Kiro secret store (`~/.kiro/secrets.json`) that Kiro has not marked as migrated, so Kiro would copy it into the helper's home | use a Claude Code or Codex helper, or an installed Kiro agent; CAO will not touch `~/.kiro` to change this |
| `kiro_v3_global_home:powers` | the CAO server runs as a user that can list an unreadable folder (root, or an ACL), so CAO cannot stop Kiro copying your powers | run the CAO server as an ordinary user without such an ACL |
| `kiro_v3_global_home:unknown` | the CAO server has no absolute `HOME` | start the server with `HOME` set |
| `kiro_v3_global_home:changed` | after kiro-cli started, the helper's powers placeholder changed or a Kiro migration marker in `~/.kiro` appeared or vanished: Kiro's legacy copy may have run | nothing was sent and the home is removed; report it, with your kiro-cli version |
| `kiro_v3_workspace_config:home` | the launch directory is your home folder, or is inside a home folder that is a git repository | launch from a repository folder, or use a worktree |
| `kiro_v3_unverified_version` | the kiro-cli version is listed but not yet operator-verified | use a verified kiro-cli version; operators running the checklist can opt in (see [Pending versions](#pending-versions-operator-only)) |
| `kiro_v3_unsupported_version` | kiro-cli is a version CAO has no schema for | use a [supported kiro-cli version](#supported-kiro-cli-versions) |
| `Kiro engine 'v3' requires wrapper flag` | the kiro-cli on the server's `PATH` doesn't list a flag the helper needs (`--trust-workspace`, or `--model` or `--effort` when asked for) | use a [supported kiro-cli version](#supported-kiro-cli-versions) |
| `kiro_home_invalid:ignored` | Kiro would ignore the helper's `KIRO_HOME` and use `~/.kiro`: the server's `HOME` is the helper's Kiro home (`ephemeral/kiro/<id>` or `ephemeral/kiro-probe` under the CAO home) or a folder inside it | set `HOME`, or `CAO_HOME_DIR`, so that `HOME` is not under `$CAO_HOME_DIR/ephemeral/` |
| `kiro_home_invalid:forwarded` | the launch forwarded its own `KIRO_HOME` in `env_vars` | don't forward `KIRO_HOME`; each helper has its own |
| `kiro_home_invalid:unsafe_path` | on herdr, the CAO home path holds characters herdr cannot pass to a pane | use a CAO home path of letters, digits and `-_./` |
| `kiro_home_invalid:exists` | something is already at the helper's home path | remove it; CAO never deletes what it did not make |
| `kiro_home_invalid:root`, `:create`, `:write`, `:seal` | the helper's home or the folder above it is unsafe (a link, not owned by you) or could not be written | check the CAO home's permissions and free space |
| `kiro_home_invalid:probe` | the probe home `ephemeral/kiro-probe` holds something CAO did not make | remove `ephemeral/kiro-probe` (see the FAQ) |
| `kiro_home_invalid:modified` | the helper's home changed between preparation and the launch, or after kiro-cli started | report it; something else writes there |
| `kiro_cli_unresolved` | no usable kiro-cli on the server's `PATH`: missing, writable by group or others, or in a folder whose path holds `=` (`/usr/bin/env` would read it as a variable) | install kiro-cli for the server's user, in a folder without `=` in its path |
| `kiro_agent_invalid:tools` | the helper's tools include something other than the five tool atoms | ask for tool atoms only |
| `kiro_agent_invalid:mcp`, `:shape`, `:unreadable`, `:name` | the helper's agent file does not name `cao-mcp-server` alone with an absolute command, carries extra fields, or could not be read back; or the helper's name is not a plain file name | report it |
| `kiro_v3_invalid:<check>` | the helper's agent file or its `cao-null` default failed CAO's v3 validator | report it, with the check named |

#### FAQ

**Does a helper change my `~/.kiro`?** No. CAO only checks whether four paths exist there
(`powers`, `secrets.json`, `.powers-migrated`, `.secrets-migrated`). It never opens,
lists, creates or changes anything under `~/.kiro`. If Kiro itself changes a marker while
a helper starts, the helper is refused before any task.

**Why is my helper refused for a secret store?** Kiro copies a secret store it has not
marked as migrated into every other Kiro home, and its log strings tie that store to MCP
sign-in state. CAO can't stop that copy without editing `~/.kiro`, which it won't do.

**Can a helper see my Kiro agents or MCP servers?** No. Its Kiro home holds only its own
agent file, the `cao-null` default and the settings seed, and its only MCP server is
`cao-mcp-server`. A helper launched in your home folder is refused, because there Kiro
would load `~/.kiro` as workspace configuration.

**I can't delete an old CAO home: "Permission denied".** Each helper home and the probe
home hold an empty folder at mode 0000, which `rm -rf` cannot enter on macOS. Run
`chmod -R u+rwx <the folder>` first, then remove it. CAO removes helper homes itself on
release.

**Is this an OS sandbox?** No. A helper with `execute_bash` runs as you and can change its
own files. CAO records at release whether its files or your `~/.kiro` markers changed.

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
