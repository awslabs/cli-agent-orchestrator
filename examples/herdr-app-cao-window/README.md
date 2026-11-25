# CAO window for the Herdr app

A second Herdr app window that shows only CAO. It has its own app home and its own herdr-core session, so your own terminals never appear in it and CAO never appears among them.

## Quick start

On a Mac (needs the Herdr app, and the `herdr-app` backend on your cao-server with no auth token; details under [Prerequisites](#prerequisites)):

```sh
git clone https://github.com/awslabs/cli-agent-orchestrator.git
cd cli-agent-orchestrator/examples/herdr-app-cao-window
hc --version; jq --version; which herdr-app                                      # hc 0.1.21 or later
defaults read /Applications/Herdr.app/Contents/Info CFBundleShortVersionString   # Herdr app 0.1.16 or later
sh install.sh --local                    # CAO runs on this Mac ...
sh install.sh --cao-ssh-host HOST        # ... or on HOST, which `ssh HOST` reaches
~/.local/bin/herdr-cao                   # open the window
sh uninstall.sh                          # remove it again
```

The installer creates the window's home `~/.config/herdr-app-cao`, the launcher `~/.local/bin/herdr-cao` and `~/Applications/CAO.app` (a copy of the Herdr app named CAO, with its own icon). It never changes your main Herdr home. The options are under [Install](#install).

## What the window shows

Each region of the window has one job:

- **Sessions** (the top block of the left sidebar) lists one line per CAO session, styled like the spaces list of Herdr's own window: a small rounded badge with the first letter of the session's name after its `cao-` prefix (`cao-ws-notes` shows `W`; every session name starts with `cao-`, so the prefix is skipped), the name, a small mark before the status mark when a session needs you (the orange `!` square) or has a reply you have not seen that its own status mark does not show (a green dot; no word is ever drawn), and at the right end the status mark of its first talkable agent (the marks of Herdr's own Agents list: a spinner for working, a ring for idle, a dot for done, the same dot in green (the theme's `ansi2`) for a reply you have not seen, a `!` square for needs you and error; an agent that is idle only because it waits for the workers it assigned shows the same spinner in the muted colour, and its tooltip reads `waiting on N workers`). The badge colour is Herdr's rule for spaces: each new session takes the colour from its palette that is used least, and keeps it (kept in the window's shared state, so it survives a restart). The text carries no weight of its own: it takes the weight of your `font_weight` setting like the rows of Herdr's own lists (the block titles are Herdr's medium 11.5 px). The words of the state (done, idle, working, replied, `needs you: <kind>`) are in the line's tooltip, not on the line. The list is in a stable order, the oldest session first (a session's age is the creation time of its oldest terminal, ties go by name, and a recorded draft session is last), so a row never jumps when a state changes: the marks, the status bar counts and the filters tell you which session needs you. The `+` in its header opens the New session dialog.
- **Agents** (the block under Sessions) shows the session you selected: "Started by schedule X" when a schedule started it, the agent's recap when it printed one (its label `the agent's own words` on a line of its own, then the text wrapped to at most three lines at the size of the agent rows and ending in `…` when it is longer; the whole text is in its tooltip; it is the one text in the left sidebar that wraps, every row stays one line), and the agents as a tree that shows who started whom. Each agent is one line, as in the Agents list of Herdr's own window: the logo of its program (the Claude Code, Kiro or Codex glyph, the same file Herdr draws from its `icons` folder, and Herdr's generic agent glyph for a program with none), its name (the name of its Herdr tab, for example `conductor-774e`), how long it has been in its state (`12m`, `3h`), its status mark at the right end and, in the slot at the right end, a pin for a worker (a push-pin: upright and filled in the accent colour while pinned, tilted and outline-only in the muted colour while unpinned). The session's main agent, the first talkable one (the conductor you talk to), wears a small crown in that same slot on its row, so the status marks stay in one column and its name keeps the room, and after its name on its top-bar tab; workers and a second talkable agent do not. Its state in words, its turn and its other facts are in the line's tooltip. The tree shows every agent of the session, each worker under the agent that started it. Every worker is always shown, however many the session has, and the block scrolls when the tree is taller than it. A pin on a worker puts its tab in the top bar; it decides nothing else. A click on an agent puts its terminal in the pane, and the row of the terminal you are in, however you got there (a click, a top-bar tab, Herdr's own agent keys, switching space), is highlighted.
- **One line per row.** A name or a path that does not fit ends in an ellipsis, and its whole text is in the tooltip; drag the left sidebar's edge (200 to 640 px, 268 px at first) to see more. A mark and an honesty label ("heuristic", "best guess", "the agent's own words") are never cut.
- **The top bar** holds the tabs of the focused session's shown agents on the left, each named like its row in the Agents block (the conductor too, and no tab carries a role tag), with the logo of its program and no status mark (the state is drawn in the Agents block and the Sessions list, and a tab's tooltip says it in words), and in the same order as those rows (a session with only its conductor shows that one tab), and, after a thin divider, the **Schedules** and **Workflows** buttons on the right, drawn like the Files and Changes buttons of Herdr's own window (an icon before the word). The tab strip scrolls sideways when the tabs do not fit (a wheel over the bar steps to the previous or next tab, as in Herdr's own tab bar) and keeps the focused tab in view; the divider and the buttons stay put. The counts are in the lists' own headers.
- **The right sidebar** is hidden when the window starts. A top-bar button opens it at the list of schedules or of workflows, a click on a row shows that item's details under a back arrow, and the same button closes it. From the details you can enable, disable, run, cancel, and resume, each after a confirmation. Schedules and workflows are global: the sidebar does not follow the selected session. What the window calls a schedule is a CAO flow: CAO's own names stay (the `/flows` routes, `cao flow` as an alias of `cao schedule`, the `cao-flow-<name>` sessions); the list title's tooltip says so.
- **The dialog** is one centered overlay that holds every form: New session, New schedule and Edit schedule, New workflow and Edit workflow, Run for a workflow, and the help of the status bar. **Draft with AI** (marked "proposed") asks a short-lived author agent to fill a schedule or workflow form. Nothing is saved without your click.
- **The status bar** counts sessions, asking agents, replies, and working sessions (a session whose agent waits on its workers counts as working, not as replied). Each count is a button: asking, replied, and working filter Sessions, and the sessions count clears the filter. The `?` button opens a help with the two notes that explain the counts.
- **The window's title** is `CAO`, so Mission Control, Cmd-` and the window switchers list this window as `CAO` and your main window as `Herdr`. The status bar's mod sets it once when it starts. The name in Cmd+Tab and the Dock, and the icon, come from the app copy the installer makes (see [The app switcher says CAO](#the-app-switcher-says-cao)).
- **The top-left of the window** shows the CAO mark and the word `CAO`, where Herdr's own window shows its logo and `Herdr`. It is Herdr's own row (the search button that opens the picker, and the fold button, with the mark in the logo's place at the same 20 px size and radius) drawn by the window's `cao-window-brand` mod; the mark is the project's own (`docusaurus/static/img/cao-mark.svg` for a light theme and `cao-mark-dark.svg` for a dark one, copies of which the mod's folder holds), drawn in full colour, and the mod picks the light or the dark one by the brightness of the sidebar's fill; the layout points only its `brand` block at it, and the sidebar's footer stays Herdr's. The fold button asks for a 76 px rail (44 px off macOS), which the sidebar's 200 px minimum clamps, so it leaves the sidebar at 200 px.

The window talks to cao-server only through `exec` on the CAO machine, against `127.0.0.1`. It opens no port and needs no port forward.

To test it step by step, follow [CHECKLIST.md](CHECKLIST.md).

## Prerequisites

You need all of these:

- `jq`, on the machine where the Herdr app runs. The installer uses it.
- herdr-core `hc` 0.1.21 or later, on the machine where the app runs. Check with `hc --version`. CAO's own host needs herdr-core 0.1.21 or later as well, because the herdr-app backend needs it.
- The Herdr app 0.1.16 or later, on macOS. The installer reads the version with `defaults read <app>/Contents/Info CFBundleShortVersionString`.
- A cao-server on the `herdr-app` backend ([docs/herdr-app.md](../../docs/herdr-app.md)), on a known port (default 9889), with no auth token. The window cannot use a cao-server that has auth on. It shows "CAO requires auth - not supported by this window yet" and disables every write control.
- `herdr-app`, the Herdr app's command-line tool, on the machine where the app runs. Check with `which herdr-app`. The app puts `herdr-app` and `hc` in `~/.local/bin` as links to the app and to herdr-core. `install.sh` does not call it. `uninstall.sh` uses it to quit the window, and CHECKLIST.md uses it for `check` and `status`.
- A POSIX `sh` to run `install.sh` and `uninstall.sh`.

A cao-server that carries the role labels and the schedule routes of the same change gives the full window. An older cao-server works with the fallbacks described under [Known limits](#known-limits).

The prerequisites that depend on where CAO runs are under each topology below.

## Topology: CAO and the app on one machine

CAO and the Herdr app run on the same Mac.

- Install with `--local`. The installer adds the CAO machine with `hc machine add --via "sh -c" --name cao --session cao`, so `hc` reaches CAO's core through a local shell and needs no ssh.
- The cao-server runs on this Mac, on the port you pass with `--cao-port` (default 9889).

```sh
sh install.sh --local
```

## Topology: CAO on a remote machine

CAO runs on another machine, for example a Linux build host. The app runs on your Mac.

- The Mac reaches the CAO host over ssh. `hc machine add` uses ssh, so `ssh <host>` must work from the Mac. See `hc machine add --help` for the forms the host argument takes.
- `hc` 0.1.21 or later must run on the CAO host too. `hc machine add --install` copies it there when it is missing or too old.
- The cao-server runs on the CAO host and listens on `127.0.0.1` there. The window reaches it with `exec` on that machine, so you open no port.

```sh
sh install.sh --cao-ssh-host build-box
```

`build-box` stands for any host name your ssh config knows. The installer runs `hc machine add build-box --session cao --name cao` in the window's own core session.

## Topology: the app on Linux

The Herdr app on Linux is unsupported. Nobody has run this window on a Linux build of the app, so `install.sh` exits with code 4 on Linux and writes nothing.

This limit is about the machine where the app runs. A Linux machine is fine as the CAO host in the remote topology.

## Install

Run the installer on the machine where the app runs, from this folder. Say where CAO runs, with `--cao-ssh-host HOST` or `--local`.

```sh
sh install.sh --cao-ssh-host build-box
```

`sh install.sh --help` lists the options. The defaults suit most setups.

| Option | Default | Meaning |
|---|---|---|
| `--cao-ssh-host HOST` | none | CAO runs on `HOST`. Pass this or `--local`, not both. |
| `--local` | off | CAO runs on this machine. |
| `--home DIR` | `$XDG_CONFIG_HOME/herdr-app-cao` (`~/.config/herdr-app-cao`) | The window's app home. |
| `--core-session NAME` | `caowin` | The window's herdr-core session. Never `cao` or `default`. |
| `--machine NAME` | `cao` | The name of the CAO machine entry in the window's core. |
| `--cao-core-session NAME` | `cao` | CAO's own core session (`terminal.herdr_app_session`). |
| `--cao-port PORT` | `9889` | The cao-server port on the CAO machine. |
| `--bin-dir DIR` | `$HOME/.local/bin` | Where the launcher goes. |
| `--launcher-name NAME` | `herdr-cao` | The launcher's file name. |
| `--app PATH` | `/Applications/Herdr.app` | The Herdr app. |
| `--app-copy-dir DIR` | `$HOME/Applications` | The folder that holds `CAO.app` (macOS only). See [The app switcher says CAO](#the-app-switcher-says-cao). |
| `--no-app-copy` | off | Do not make `CAO.app`: the launcher opens the Herdr app itself, and the app switcher and the Dock say Herdr. |
| `--main-home DIR` | `$XDG_CONFIG_HOME/herdr-app` (`~/.config/herdr-app`) | Your normal Herdr home. The installer only reads it. |
| `--telemetry on\|off` | unset | Sets telemetry in the window's `settings.json`. |
| `--prop KEY=JSON` | none | Stamps one more layout prop on every CAO block. Repeatable. |
| `--force` | off | Replaces a layout you customized. |

What the installer does:

1. Refuses a core session named `cao` or `default`, or equal to `--cao-core-session`. Refuses a home that is, holds, or lies inside your main Herdr home, `$HERDR_APP_HOME`, `$HOME`, or the launcher folder. Refuses an empty `--home`, `--main-home`, `--bin-dir`, or `--app`.
2. Checks `hc` (0.1.21) and the app (0.1.16). It exits with code 3 when either is too old.
3. Derives the window's layout from your main home's `layouts/main.json`. When that file is missing, it uses the copy in this folder and warns that the copy may lag your Herdr.
4. On macOS, makes `CAO.app` (see [The app switcher says CAO](#the-app-switcher-says-cao)) before it writes anything else. It refuses with exit 2 when `CAO.app` already exists and is not a copy it made, and with exit 3 when `codesign`, `xattr` or `PlistBuddy` is missing.
5. Writes the home: the layout, the mods (each mod's `.js` files, `mod.json` and its own `.svg` drawings, the push-pins and the crown of the side mod, the two button icons of the tabs mod and the two drawings of the CAO mark of the brand mod; the drawings were written for this window, so they carry no third-party notice, and Herdr's own icons are not copied because every Herdr home has them), `shared.json` (selects the layout and skips the welcome screen), and `settings.json` copied from your main home. When that `settings.json` names a theme and your main home has `themes/<name>.json`, the installer copies that file to `<home>/themes/` as well, so Herdr can apply the window's layout. It keeps any file it replaces as `<file>.bak-<epoch>`. It sets the home to mode 700 (owner only) on every run, because the manifest names the launcher and the core session that `uninstall.sh` acts on. If `--home` points at a folder that already exists, the installer sets that folder to 700 as well.
6. Adds the CAO machine to the window's core when `hc machine ls --json` there does not list it.
7. Writes the launcher and the manifest `<home>/cao-window-install.json`, which records the options, the SHA-256 of every file written, and the app copy (its path, the Herdr version it was made from, and the hash of its icon).

Before it writes anything, the installer stages every file in a temporary folder and checks every target. A refusal leaves nothing behind.

The installer fetches nothing from the network. It never writes to your main home, and it never changes CAO's core session or any CAO terminal.

A run with the same options changes nothing and prints `install: up to date`. The last lines of a normal run say which switches are in effect and where the launcher is:

```
install: toast mod off, mod imports: copy
install: done. Open the window with: <bin-dir>/herdr-cao
```

If your main home has no `settings.json`, the installer also prints that a fresh Herdr home turns telemetry on by default. Pass `--telemetry off` to turn it off.

If the `settings.json` names a theme and your main home has no `themes/<name>.json`, the installer copies nothing and prints one `install: note:` line that says the theme was not found. The installer does not know which themes Herdr ships, so the line also appears for a built-in theme, where it is harmless.

### The two environment switches

Two switches come from the environment, not from options. Each records the outcome of an experiment on the Herdr app, so the installer tests set them. Both are off by default. The summary line prints the value in effect, so a variable left exported in your shell shows.

| Switch | Default | Effect |
|---|---|---|
| `TOAST_MOD=1` | `0` (off) | Installs the `cao-window-toast` mod and sets the layout's root `toast` key to it. |
| `MOD_IMPORTS=bundle` | `copy` | Writes one `main.js` per mod, with the shared modules inlined, instead of copying `lib/*.js` flat into each mod folder. |

**`TOAST_MOD`.** An experiment on the toast contract found that a mod named by the layout's root `toast` key receives every notification, so `TOAST_MOD=1` works. It is not the default for this reason: the toast mod in this folder only redraws Herdr's own toasts. It does not hold them or release them on a hand-off, because the experiment that decides whether holding works needs a person to run it for 30 minutes and has not run. With the mod on, every notification, including the alert for a blocked agent, depends on it. With the mod off, Herdr's own toasts show unchanged, and the Dock badge shows the number of asking agents. Set `TOAST_MOD=1` when you want to try the window's own toast. Set it on every installer run: a run without it derives the layout without the `toast` key.

```sh
TOAST_MOD=1 sh install.sh --cao-ssh-host build-box
```

**`MOD_IMPORTS`.** Every mod imports the shared modules from its own folder (`./logic.js`). An experiment confirmed that Herdr loads such imports, so the default `copy` stands. Set `MOD_IMPORTS=bundle` only if a later Herdr stops loading them, which shows as an import error when a mod loads. Bundle mode leaves only `mod.json` and `main.js` in each mod folder. In either mode the installer copies only a mod's `mod.json` and `.js` files, so a stray file in the source folder (a sync tool's temp file, a note) is not installed or listed in the manifest.

## Configure the window

The layout carries these props on every CAO block. The installer stamps the defaults and the two names you pass as options. Change one with `--prop KEY=JSON` and run the installer again.

| Prop | Default | Meaning |
|---|---|---|
| `machine` | `cao` | The CAO machine's name in the window's core. Set by `--machine`. |
| `caoPort` | `9889` | The cao-server port. Set by `--cao-port`. |
| `talkableProfiles` | `[]` | Profile names that count as talkable even when the terminal has a caller. |
| `hiddenRootProfiles` | `["memory_manager"]` | Root profiles that are not talkable. They count as workers. |
| `quietMinutes` | `10` | Minutes without a status change before a working worker gets "stuck?" (labelled "heuristic"). |
| `holdMaxSeconds` | `120` | The longest a held "asking" notification waits. No effect today: the shipped toast mod holds nothing. |
| `draftProfile` | none | The CAO agent profile the Draft with AI author runs as. With none set, Draft explains how to set one. See [Draft with AI needs an author profile](#draft-with-ai-needs-an-author-profile). |
| `draftProviders` | `["claude_code"]` | The providers an author may run on. |
| `contextPercentRegex` | none | A regular expression for the context percentage on an agent's screen. With none set, no percentage shows. |

A **talkable** agent is a terminal you are meant to talk to: by default, a session's root agent, which has no caller. Every other terminal is a worker.

Examples:

```sh
sh install.sh --cao-ssh-host build-box --prop 'talkableProfiles=["thread-coordinator"]'
sh install.sh --cao-ssh-host build-box --prop 'draftProfile="draft-author"'
sh install.sh --cao-ssh-host build-box --prop 'contextPercentRegex="ctx (\\d+)%"'
```

The `--prop` value must be JSON. A value that is not JSON exits with code 2 and writes nothing.

Notes on specific props:

- **`draftProfile`** has its own section below: Draft refuses to start unless the profile meets three rules.
- **`contextPercentRegex`** is a JSON string. The first capture group must hold the number, from 0 to 100. It depends on what your agent's screen prints, so test it against your own screen.

Edit the layout by hand and the next install refuses to overwrite it. Re-run with `--force` to keep your copy as `layouts/cao-window.json.bak-<epoch>` and write the derived layout.

## Open the window

Run the launcher that the installer printed:

```sh
~/.local/bin/herdr-cao
```

On macOS the launcher runs `open -n -a <app> --env HERDR_APP_HOME=<home> --env HC_SESSION=<core>`, where `<app>` is `CAO.app` while that folder exists and the Herdr app when it does not. You get a second tile in the Dock, named CAO. Run the launcher again to bring the same window back: the app starts no second process for the same home. (The checklist verifies this on a real Mac.)

Quit the window with Cmd+Q inside it, or from a terminal:

```sh
HERDR_APP_HOME=~/.config/herdr-app-cao herdr-app do app.quit
```

Keep in Dock works for the CAO tile now. It pins `CAO.app`, a bundle of its own, so the pinned tile is separate from Herdr's: the window the launcher opens lights up the pinned CAO tile, and your main window keeps the Herdr tile. A click on the pinned tile starts `CAO.app` like any app, without the home and core session the launcher passes, so open the window with the launcher. (After `--no-app-copy` the tile is the shared Herdr bundle: pinning it pins Herdr, and a click would open your main window, so do not pin it then.)

## The app switcher says CAO

Cmd+Tab, the Dock and Mission Control take an app's name and icon from its bundle, not from a window. The window's title is `CAO` (the status bar's mod sets it), but one Herdr bundle can only be named Herdr. So on macOS the installer makes a second bundle:

- It clones the Herdr app to `~/Applications/CAO.app` (`cp -cR`, an APFS clone: the 59 MB are shared with the Herdr app until one of them changes; a volume that cannot clone gets a plain copy), sets `CFBundleName` and `CFBundleDisplayName` to `CAO` and `CFBundleIdentifier` to `dev.herdr.app.cao`, points `CFBundleIconFile` at `CAO.icns` (this example's icon, copied into the bundle; it is derived from the CAO mark SVGs and uses only Herdr's colours and the shape of its tile, see [Rebuilding the icon](#rebuilding-the-icon)), strips extended attributes (`xattr -cr`) and signs the bundle ad hoc (`codesign --force --deep -s -`).
- It records the Herdr version the copy came from in the bundle's Info.plist, as `CAOWindowSourceVersion`. That key is how the installer and the uninstaller know the folder is theirs: they refuse to replace or remove a `CAO.app` without it, and they remove only an absolute path to a folder named `*.app`, never a link.
- A rerun rebuilds the copy only when the Herdr app's version differs from the recorded one, or when the copy is gone. A rerun with the same Herdr changes nothing and prints `install: up to date`.
- `uninstall.sh` removes the copy.

What it costs, and what to know:

- **An update of Herdr does not update the copy.** Herdr's updater replaces `Herdr.app`. If you run the CAO window from `CAO.app` and the updater runs inside it, the updater may change the copy in place (its name and signature come back from the new release, or the update fails). After a Herdr update, run the installer again: it sees the new version and rebuilds the copy.
- **The signature is ad hoc.** The original is signed with a hardened runtime and Herdr's team. The copy has no team: It is not notarized and it is not Herdr's signed build. The install strips the quarantine attribute (`xattr -cr`), and `codesign -v` checks the result. macOS keys its permissions by app identity, so a permission you gave the Herdr app (microphone, camera, automation) is probably asked again for `CAO.app`.
- **Disk.** 59 MB on a volume that cannot clone, close to nothing on APFS until the two diverge.
- **Herdr's own window still says Herdr.** The copy is used only by the launcher.
- **The window's title.** The status bar's mod still sets the window's title to `CAO` (`setWindowTitle`); the bundle name is a second, separate setting.

`--no-app-copy` skips all of it and keeps opening the Herdr app. It does not remove a copy an earlier run made: run `uninstall.sh`, or delete `CAO.app` yourself.

## Remove the CAO machine from your main window

This step is required before you rely on the window. Your main window and the CAO window both show the CAO terminals while the main window's core session lists the CAO machine. Both windows then write the same CAO structure document ("the newest write wins"), and both ring alerts for the same agents. Two experiments did not run: one that tests two windows writing one document and one that tests the updater with two processes. The safe default therefore applies: remove the CAO machine from the main window's core session. CHECKLIST.md (U19) runs exactly that two-writer state once, while the CAO machine is still in your main window: it asks you to rename and reorder a tab and to report any lost or duplicated tab.

The installer never does this. Do it by hand, after the CAO window works (run the checklist first):

1. In a terminal that is not inside the CAO window, list the machines of your main core: `hc machine ls`. Note the name of the CAO machine and the host it points to. Leave `HC_SESSION` unset so you read the main core.
2. Remove it: `hc machine rm <name>`.

CAO's terminals keep running ("Forget a machine (its terminals keep running there)", from `hc machine --help`). The main window stops showing CAO spaces, and it stops alerting for blocked CAO agents. The CAO window's Dock badge and toasts cover that. Your main window may also forget the focus and attention state it kept for CAO terminals.

To roll back, add the machine to the main core again, with the same host and CAO core session:

```sh
hc machine add <ssh-host> --session cao --name <name>
```

For a CAO on the same Mac, use `hc machine add --via "sh -c" --name <name> --session cao`. Use your own CAO core session name in place of `cao` if you changed `terminal.herdr_app_session`.

One more effect of separate cores: a space or tab you create on "this computer" from the CAO window lands in the window's own Mac-local core (`caowin`), not in CAO's.

## Draft with AI needs an author profile

Set `draftProfile` in the window's props to the name of a CAO agent profile. Draft refuses to start unless the profile:

1. declares exactly one MCP server, `cao-mcp-server`, in `mcpServers`. A profile that declares none is refused too: with no declared server, Claude Code starts with your own MCP servers (user level, project `.mcp.json`, and plugins);
2. does not set `native_agent`. A native agent keeps its MCP configuration in Claude's own agent file, which the window cannot read;
3. has a provider the window can restrict: native enforcement, not an install-time provider (`kiro_cli`, `opencode_cli`), listed in `draftProviders` (default `["claude_code"]`), and installed on the CAO machine.

The window launches the author with `allowed_tools=fs_read`, so it gets only read access plus CAO's own tools. A profile with no `provider` field resolves to `kiro_cli`, which is refused, so declare `provider: claude_code`. Draft also needs a working directory: launch one session from New session first, and the window remembers its directory.

Example front matter (save it as `draft-author.md`, install it with `cao install ./draft-author.md`, then run the installer with `--prop 'draftProfile="draft-author"'`):

```yaml
---
name: draft-author
description: Writes a flow or workflow definition as text
provider: claude_code
role: reviewer
mcpServers:
  cao-mcp-server:
    type: stdio
    command: cao-mcp-server
    args: []
---
```

When a rule fails, Draft shows the reason in its box and sends nothing:

- no `draftProfile`: `Set draftProfile in the layout props to the profile the author should run as ...`;
- no server declared: `The author profile <name> must declare exactly one MCP server, cao-mcp-server ...`;
- another server declared: `The author profile <name> declares an MCP server other than cao-mcp-server ...`;
- a native agent: `The author profile <name> is a native agent ...`;
- a provider that fails rule 3: a line naming the provider and the reason.

Limits you accept when you use Draft:

- **Agent Plugins.** Use an installed profile that declares only `cao-mcp-server`, and install no Agent Plugin that brings an MCP server on the CAO machine. CAO merges a plugin's MCP servers into a launch (`with_plugin_mcp`), and they do not show on the profile read, so the window cannot refuse them. A CAO-side follow-up would make a restricted `claude_code` launch always pass `--strict-mcp-config` with only the granted servers.
- **Your own Claude setup.** The author also runs with your own Claude configuration, as any `cao launch` in that directory does: user and project hooks and skills, `CLAUDE.md`, the working directory's `.claude/settings.json`, and built-in tools outside CAO's deny list, such as `Skill`. Treat the working directory as trusted.
- **Providers.** Only `claude_code` is verified as an author provider.
- **Reads.** With this profile the author can read any file you can read (`Read`). What it finds can surface only in the form fields, which you read before any save.
- **Callbacks.** `send_message` and the other CAO callback tools stay reachable by the author.

## Tool policy and YOLO

New session has three policies: **Ask first** (a dialog shows the profile's declared tools and the provider's enforcement), **Auto-approve** (no dialog), and **YOLO** (you type the session name to confirm).

An allowed-tools override in Advanced that contains a bare `*` as one of its items is a YOLO launch, whatever policy you chose. It needs the session name and the typed confirmation. A glob inside a name (`fs_*`) is a name, not `*`.

A broad named list, for example `fs_*,execute_bash,web_fetch`, does not take the typed dialog. Such a list is as powerful as YOLO and skips the typed confirmation. Treat it that way.

## Sounds, the updater, and the process list

**Sounds.** Herdr plays its alert sound before any toast mod runs, so the window cannot hold a sound. Sounds are a Herdr setting in `<home>/settings.json`. The CAO window's home copies your main home's `settings.json` when it is first installed, so it starts with your choice. Change the CAO window's setting in its own Herdr settings if you want silence there.

**The updater.** Both windows use one Herdr updater. Herdr has no per-home switch to turn updates off, and an update's Restart does not bring the CAO window back. After a Herdr update, run the installer again (it rebuilds `CAO.app` from the new Herdr, see [The app switcher says CAO](#the-app-switcher-says-cao)) and reopen the CAO window with the launcher.

**The process list.** The window sends request bodies to cao-server as a curl argument, through `exec` on the CAO machine. For as long as that curl runs, anyone who can list processes on the CAO machine can read the body. Bodies are initial messages, schedule prompts, workflow source, workflow run inputs, and Draft descriptions. Do not type a secret into any of them. Env values are never sent.

**Why env values are not sent.** Environment values are where secrets go. `exec` has no way to feed a request body on stdin, so the window never puts env values in a request. When you fill an env row in New session, Launch is disabled and the form shows the `cao launch ... --env KEY=VALUE` command to run in a terminal on the CAO machine, where the CLI sends the values itself.

**Body size.** Linux limits one argument to 128 KiB, so the window refuses a request body over 122880 bytes (120 KiB, JSON-encoded). The refusal names the CLI command that does the same write.

## Upgrade

Pull this repository, then run the installer again with the same options.

```sh
git pull
sh install.sh --cao-ssh-host build-box
```

- A file that differs is kept as `<file>.bak-<epoch>` and replaced. If that name is taken, the installer uses `<file>.bak-<epoch>-<n>`.
- The installer derives the layout again from your main home's current `layouts/main.json`. A layout you did not change is replaced, with a backup. A layout you customized makes the run exit with code 2 and write nothing, until you pass `--force`.
- If a Herdr update changed the shape of `main`, the installer exits with code 5 and names the block it cannot find. Nothing is written.
- After the install, quit the window and open it again with the launcher.

## Uninstall

```sh
sh uninstall.sh
```

`uninstall.sh` takes `--home DIR` and `--main-home DIR`, with the same defaults as the installer. It reads the manifest in the window's home and then, in order:

1. quits the window when its control socket is live;
2. removes the CAO machine from the window's core session (it skips this when the machine is already absent);
3. stops the window's core session;
4. removes the launcher, but only a file that carries the installer's header line (`# Written by examples/herdr-app-cao-window/install.sh. Re-run the installer to change it.`). Any other file, a symbolic link, or a folder at that path stays in place with a note, and the uninstall still completes;
5. removes `CAO.app`, the path the manifest names, but only a real folder (not a link) named `*.app` whose Info.plist carries `CAOWindowSourceVersion`; any other path makes the run exit with code 2 before it changes anything, and the folder is deleted, not moved aside, because it holds nothing of yours and a rerun of `install.sh` builds it again;
6. renames the home to `<home>.removed-<epoch>`.

It never touches your main home, CAO's core session, or any CAO terminal. CAO keeps running. If `hc machine rm` or `hc server stop` fails, the script exits with code 1 and keeps the home, so you can fix `hc` and run it again.

Run it from a terminal outside the CAO window. It refuses when `$HERDR_APP_HOME` points at the window's home.

It reads the manifest again as the installer would accept it. If the manifest's launcher name contains `/`, or its core session or machine name breaks the installer's name rule (letters, digits, dot, dash, underscore, no leading dash), it exits with code 2 and touches nothing.

It stops the core session named in the manifest. If you installed with a `--core-session` that already existed and held other machines, uninstall stops that core too. Use a core name you have never used (the default `caowin` is fine on a first install).

A hand-edited manifest could still name a regular file that carries the installer's header line, and uninstall would remove it. Keep the window's home private (the installer sets it to mode 700) and do not edit the manifest.

To undo the main-window step, see [Remove the CAO machine from your main window](#remove-the-cao-machine-from-your-main-window). Uninstalling does not re-add the machine to the main core.

## Troubleshooting

### The installer exits

| Exit | Message starts with | Cause and fix |
|---|---|---|
| 2 | `say where CAO runs` | Pass `--cao-ssh-host HOST` or `--local`. |
| 2 | `refusing core session` | `cao` is CAO's own core and `default` is your normal Herdr core. Pick another `--core-session`. |
| 2 | `refusing --home` | The home is, holds, or lies inside your main Herdr home, `$HERDR_APP_HOME`, `$HOME`, or the launcher folder. Pick another `--home`. |
| 2 | `... was changed since the last install; nothing was written` | You edited the layout. Run again with `--force`. |
| 2 | `--prop ...: the value is not JSON` | Quote the value as JSON: strings need inner double quotes. |
| 3 | `hc ... is too old` | Update herdr-core to 0.1.21 or later. |
| 3 | `Herdr app ... is too old` | Update the Herdr app to 0.1.16 or later. |
| 4 | `the Herdr app on Linux is unsupported` | The app runs on macOS only. See the Linux topology. |
| 5 | `... does not look like Herdr's main layout` | Herdr changed its `main` layout. The message above it names the missing block. Nothing was written. Report it. |
| 1 | `hc machine add failed; the files are written` | Fix `hc` or ssh, then run the installer again. It adds only the missing machine. |

### The window

- **The window shows your own spaces, or nothing.** The launcher lost `HC_SESSION`. Open the launcher file and check that it passes `--env HC_SESSION=caowin` (or your `--core-session`).
- **"CAO machine not connected" in the Sessions header.** The window's core cannot reach the CAO machine. Run `HC_SESSION=caowin hc machine ls` and check `ssh <host>`.
- **"CAO unreachable, retrying".** cao-server does not answer on the CAO machine. On that machine, run `curl -s http://127.0.0.1:9889/health`. Check that cao-server runs and that `--cao-port` matches.
- **"CAO requires auth - not supported by this window yet".** cao-server has auth on. Turn auth off, or use the CLI.
- **Next runs or Edit labelled "needs a CAO change", and Create schedule disabled.** The cao-server is older than this window and lacks `POST /flows/validate`. Update CAO, or create the schedule with `cao schedule add` as the form says.
- **Draft refuses to start.** Read the reason in the Draft box. The profile must meet the three rules in [Draft with AI needs an author profile](#draft-with-ai-needs-an-author-profile).
- **"settings are not being saved".** Herdr's storage failed. The window keeps working with in-memory state and loses pins, folds, and the selection when it closes.
- **A mod fails to load with an import error.** Install again with `MOD_IMPORTS=bundle`.
- **The window did not come back after an update.** Open it with the launcher.
- **List the CAO machine's terminals** to compare with the window: `HC_SESSION=cao hc ls` on the CAO host.

### Run the automated checks

```sh
for f in $(find . -name '*.test.mjs' | sort); do node "$f" || echo "FAILED: $f"; done
```

From the repository root, the same files run under pytest:

```sh
env -u CAO_LOG_LEVEL uv run pytest --no-cov -p no:cacheprovider -q test/examples/test_herdr_app_cao_window_node.py
```

Unset `CAO_LOG_LEVEL` for the CAO test suites. Run one pytest at a time. On Linux you can prefix the command with `flock /tmp/cao-window-pytest.lock` so a second run waits for the first. macOS has no `flock`, so skip the prefix there.

## Rebuilding the icon

The CAO mark SVGs in `mods/cao-window-brand/` (`cao-mark.svg` and `cao-mark-dark.svg`) are copies of the files of the same names in `docusaurus/static/img/`. `assets/CAO.icns` is the icon of the app copy the installer makes (see "The app switcher says CAO"). It is built from `assets/cao-icon.svg`: the tile of Herdr's own icon (an 824 px rounded square in a 1024 px canvas with a transparent margin, warm off-white `#F4F1E9`, a thin grey edge `#C9C6BD`, glyph charcoal `#303438`, all read from the pixels of Herdr's icon) holding the CAO mark of this project (`docusaurus/static/img/cao-mark.svg`) at 59 percent of the tile's height. The icon is derived from the CAO mark SVGs using only Herdr's colours and the tile's shape; none of Herdr's artwork is used. The ring and the edges are charcoal and the three nodes keep the mark's brand colours, because in one colour the edges and the nodes ran together at small sizes.

You only rebuild it when you change the drawing. It needs a Mac (macOS 13 or later; `swift`, `sips` and `iconutil` come with the system and the command line tools). `qlmanage -t` draws an SVG on an opaque white background, so `assets/render-svg.swift` renders it with AppKit instead and keeps the transparent margin. From this folder:

```sh
cd assets
work=$(mktemp -d)
swift render-svg.swift cao-icon.svg "$work/cao-1024.png" 1024
mkdir "$work/CAO.iconset"
for s in 16 32 128 256 512; do
  sips -z "$s" "$s" "$work/cao-1024.png" --out "$work/CAO.iconset/icon_${s}x${s}.png"
  sips -z "$((s * 2))" "$((s * 2))" "$work/cao-1024.png" --out "$work/CAO.iconset/icon_${s}x${s}@2x.png"
done
iconutil -c icns "$work/CAO.iconset" -o CAO.icns
```

Open `"$work/cao-1024.png"` to look at it before you commit the new `CAO.icns`. `install.test.mjs` checks that the file is an icns with every size, that the source keeps Herdr's tile numbers and colours, and that every coordinate of the glyph is one of `cao-mark.svg`'s.

## Known limits

- **A cao-server without `POST /flows/validate` cannot create a schedule from the window.** Its `POST /flows` arms the new schedule and overwrites the file of a schedule with the same name, and the window cannot check either first. On such a server **Create schedule** and **Create and enable...** are disabled, the form shows the label `server checks skipped`, and it says: "This server cannot check a schedule first, and its create would arm the schedule and overwrite a schedule with the same name. Create it in a terminal on the CAO machine with `cao schedule add <file>`, then keep it from running with `cao schedule disable <name>` until you enable it here." Edit and next runs show `needs a CAO change`. As a last guard, if a server answers a create with `enabled: true` after you asked for disabled, the window warns: "This server ignored enabled: false and created the schedule enabled. Use the switch to disable it if you did not mean that."
- **A profile named `search`, `templates`, or `schema` cannot be read through `GET /agents/profiles/{name}`.** The server declares routes with those names before the `{name}` route, and the route order is the cause (read from the server source, not run). The Ask-first dialog reads the profile, so for such a profile it says the profile could not be read (`could not read the profile`, or the server's words) instead of opening. Auto-approve and YOLO do not need the read.
- **A workflow named `runs`, or longer than 64 characters, is refused.** The server uses `/workflows/runs` for the run list, and its name rule allows 1 to 64 characters of letters, digits, `-`, and `_`. The editor tells you to pick another name.
- **Python workflows.** New workflow accepts a Python script as pasted source, and Edit works on one. A Python edit goes through `PUT /workflows/{name}` with the content hash read when you opened Edit. If the workflow changed on the server meanwhile, the save is refused, your text stays, and the editor offers Reload. A YAML spec's own `name:` line must equal the workflow's name, or Save stays disabled.
- **Installing a workflow from a file on the CAO machine** is not offered. Use `cao workflow create <name> --from-file <path>` for a Python file. A YAML spec has no create-from-file command: paste it into the editor.
- **Edit works only on schedules whose file is under `CAO_HOME_DIR/flows`.** Others answer 409 on save. An edit rewrites the schedule file and drops YAML comments, but it keeps every key the window does not edit, `script` included. The window cannot create a schedule with a script. Use `cao schedule add`.
- **Schedules.** A schedule longer than 256 characters is refused. A schedule that does not fire within 41 years reads "this schedule never fires". The server's scheduler (APScheduler) counts the day of week from Monday as `0`, so `1-5` means Tuesday to Saturday. Write `mon-fri`: the presets use names, and the form warns about a digit.
- **Schedules fire only while cao-server runs,** in the server's local time.
- **No schedule run history.** CAO records only the last run time. The window lists the live `cao-flow-<name>` session and says that history needs a CAO change.
- **Replied.** An agent has replied when its turn ended and its `herdr.seen` label is below its `turn`. Without that label, and without a value this window recorded, the agent is never counted as replied, and its line in the Agents block shows "seen: unknown". The window writes `herdr.seen` with `hc label` when you focus a finished agent, checks that the write landed within 5 s, and otherwise keeps a per-window record for the rest of its run.
- **Waiting on workers.** CAO records who waits on whom: `assign` (unless `expect_callback` is false) and `send_message` with `expect_reply` open a delegation edge that the reply, an auto-forward or the worker's end closes. On the herdr-app backend CAO keeps the label `cao.waiting` on the waiting terminal at the number of its open edges, and removes it when there are none; the window reads it from the labels Herdr already pushes, with no request of its own. An idle or done agent with a `cao.waiting` count shows the muted spinner and "waiting on N workers", is not counted as replied, and its session counts as working. A `handoff` needs none of this: the caller stays working. A cao-server that is older, or runs another backend, writes no label, and the window then derives the wait from who started whom: an agent that is idle or done while a worker below it (a worker, or a worker's worker) is working or blocked shows the same mark and `waiting on N workers`, N being the unfinished workers below it, so it works with no restart of CAO. A label count above 0 wins over the tree's count. CAO corrects a label it missed within one 30 s sweep.
- **Known gap.** An agent that finished by asking you a question in prose is not counted as asking. The `?` help of the status bar says so.
- **The right sidebar is hidden at every start.** Herdr keeps whether a block is shown across restarts, so the window hides the sidebar a moment after it opens, even when you quit with it open. Open it with the Schedules or Workflows button, or with the "Started by schedule X" link. If Herdr ignores the window's show or hide command, the window sends it once more and then follows what Herdr shows.
- **Esc and Tab in the dialog are not verified.** The forms open in a Herdr `dialog` overlay. Whether Esc reaches the window before Herdr closes that overlay, so that "Discard this form?" can ask first, and whether Tab moves through the fields in the order they are drawn, have not been tested by a person with the window active (CHECKLIST U21 to U23). If Esc closes the dialog at once, text typed in it is lost. The fallbacks (a `window` overlay, a wrapper around each field, or forms in the widened right sidebar) are not built.
- **The dialog can refuse to open for up to 10 seconds.** Herdr closes some overlays without telling the window (a layout switch, quitting). The window keeps a record that the dialog is open and the open dialog renews it every second, so after such a close the `+` controls are refused for at most 10 seconds. A dialog left open while the Mac sleeps may be taken for closed.
- **A dialog cannot be closed while it sends a write** (launch, save, submit). It says "A request is in progress. Wait for the answer." and allows the close once the request's own time limit plus 5 seconds has passed.
- **Workflow step sessions** show as ordinary sessions. The window does not link them to their run.
- **A cao-server without the role labels** (older, or a restored plain window) makes the window fetch `GET /terminals/{id}` once per terminal to learn its profile and caller. A failed fetch retries after 30 s or more.
- **Recap and context percentage** read the tail of an agent's screen. The recap is a `recap:` line after the character U+203B, and it shows labelled "the agent's own words" (the label on its own line, then the text over up to three lines). The percentage is off until you set `contextPercentRegex`.
- **Toast holding is not built.** With `TOAST_MOD=1` the toast mod draws every notification as it arrives. The hold-until-hand-off behavior exists as tested logic only.
- **Guesses are labelled.** Every surface that is a guess, a heuristic, inferred, the agent's own words, proposed, or missing in CAO carries a fixed label in the window: "best guess", "heuristic", "held while you were busy", "known gap: a done agent asking in prose is not counted", "the agent's own words", "inferred from the session name", "needs a CAO change", "server checks skipped", "proposed", "AI", "declared by the profile; the server resolves the final set", "not enforced by this provider", and "inputs not described by the spec".

## What this does not do

- It does not approve a workflow plan. A first run that needs approval shows the plan id and `cao workflow approve <plan_id>` to run in a terminal.
- It does not answer an agent's prompt. Answer in the agent's own pane.
- It does not record schedule run history.
- It does not send env values. New session shows the `cao launch` command instead.
- It does not choose a Kiro engine or resume a Claude Code conversation. New session has no field for `--engine` or `--resume-session-id`: a launch uses the engine and setup CAO has for the profile. Use `cao launch --engine ...` or `--resume-session-id ...` in a terminal.
- It does not save, enable, or run anything from a Draft with AI. Only your click does.
- It does not delete schedules or workflows. It deletes only the author sessions it started for a draft.
- It does not install a workflow from a path on the CAO machine, and it does not set a schedule's `script`.
- It does not work with an authenticated cao-server.
- It does not run on the Herdr app for Linux.
- It does not change your main Herdr home, CAO's core session, or CAO's terminals. The one step that changes your main window, removing the CAO machine, is yours to do.
- It does not schedule workflows itself. Workflows have no scheduler: Schedule... creates a schedule that runs the workflow through an agent.
