# Herdr app backend

> **Experimental.** The `herdr-app` backend is opt-in. The default backend is [tmux](tmux.md).

## What it is

With the `herdr-app` backend, CAO runs its terminals in a dedicated herdr-core (`hc`) session, and the Herdr desktop app shows them: one space per CAO session and one tab per CAO terminal, named after the CAO session and window. herdr-core reports each agent's status natively (`working`, `blocked`, `idle`, `done`), so CAO delivers inbox messages from herdr-core's event stream instead of polling terminal output.

This backend is separate from the [herdr TUI backend](herdr.md) (`terminal.backend: "herdr"`), which talks to the `herdr` terminal multiplexer.

## Prerequisites

- herdr-core 0.1.21 or later on the machine where CAO runs. Check with `hc --version`. CAO looks for `hc`, then `herdr-core`, on `PATH`.
- The Herdr desktop app, if you want to see the terminals. CAO works without it.

## Enable it

Pick one:

- `cao config set terminal.backend herdr-app`
- `CAO_TERMINAL_BACKEND=herdr-app` in the cao-server's environment
- `cao-server --terminal herdr-app`

The herdr-core session defaults to `cao`. To use another one, run `cao config set terminal.herdr_app_session <name>` or set `CAO_HERDR_APP_SESSION`. cao-server starts that session's core when it isn't running. It never uses your `default` session, the one the app opens for your own terminals.

Check it took effect with `GET /health`: `terminal_backend` is `"herdr-app"`, and `components["herdr-core"]` is `"ok"` when `hc` or `herdr-core` is on `PATH` (`"unavailable"` otherwise).

## Show it in the app

Add the CAO core as a machine on the machine where the app runs:

- CAO on a remote host: `hc machine add <ssh-host> --session cao`
- CAO on the same machine as the app: `hc machine add --via "sh -c" --name cao-local --session cao`

Use your `terminal.herdr_app_session` value in place of `cao` if you changed it.

## A dedicated CAO window

If you add CAO as a machine in your main window, its sessions mix with your own work. [`examples/herdr-app-cao-window/`](../examples/herdr-app-cao-window/) is an example of a second Herdr window with its own app home and its own herdr-core session. That window lists only CAO. Its [README](../examples/herdr-app-cao-window/README.md) opens with a Quick start (prerequisites, install, open, remove) and covers every option, the app switcher name and icon, upgrade and uninstall. Each region of the window has one job:

- **Sessions and Agents**, the two blocks of the left sidebar. Sessions lists one line per CAO session (a status glyph, its name and, for a session that needs you or has an unseen reply, a `!` square or a green dot; no word is drawn, the words are the tooltip), and the list keeps a stable order, the oldest session first, so no row jumps when a state changes (the marks, the status bar counts and its filters carry the attention). Agents shows the selected session as a tree of who started whom, one line per agent named like its Herdr tab. The words of a state (done, idle, working) are in the line's tooltip, and a name that does not fit ends in an ellipsis; drag the sidebar's edge to widen it. It lists every agent of the session; only a session of more than 12 agents folds its deepest workers into "N workers", and a pin on a worker puts its tab in the top bar.
- **The top bar** holds the tabs of the focused session's shown agents, and the **Schedules** and **Workflows** buttons.
- **The right sidebar** starts hidden. A top-bar button opens it at the list of schedules or of workflows, and a row shows that item's details. You can create (disabled), enable, disable, run, and edit schedules, and run, cancel, resume, create, and edit workflows. Schedules need the routes `POST /flows/validate` and `PUT /flows/{name}`. On a cao-server without `POST /flows/validate`, Create is disabled (that server's create would arm the schedule and overwrite a schedule with the same name), and the form points to `cao schedule add` and `cao schedule disable`. A first workflow run that needs plan approval shows the `cao workflow approve` command to run in a terminal: the window has no approve button.
- **The dialog** is one centered overlay for every form: a new session, a schedule, a workflow, and a workflow run. Whether Esc and Tab behave as designed in that overlay has not been verified on a Mac.
- **The status bar** counts sessions, asking sessions, replies, and working sessions. Each count filters the Sessions block, and its `?` button explains the counts.

**Draft with AI** (marked "proposed") fills a schedule or workflow form from an author session that the window deletes afterward. You set up the author: `draftProfile` must name a CAO agent profile that declares exactly one MCP server, `cao-mcp-server`, sets no `native_agent`, and runs on a native, non-install-time provider that is installed and listed in `draftProviders`. The window reads CAO through `curl` on the CAO machine. Request bodies (messages, schedule prompts, workflow source, run inputs, and Draft descriptions) are visible in `ps` on the CAO machine while curl runs, and environment values are never sent. It needs a cao-server on the `herdr-app` backend with no auth token. See the example's [README](../examples/herdr-app-cao-window/README.md) for prerequisites, topologies, limits, and troubleshooting, and its [CHECKLIST](../examples/herdr-app-cao-window/CHECKLIST.md) for a step-by-step test.

- Run `install.sh` from that folder on the machine where the app runs. Use `--cao-ssh-host <host>` when CAO runs on another machine, or `--local` when it runs on the same machine as the app. `install.sh --help` lists every option.
- The installer needs `jq` and `hc` 0.1.21 or later, and it fetches nothing from the network. It writes a launcher, `herdr-cao` in `~/.local/bin` by default, and on macOS a copy of the Herdr app named `CAO.app` in `~/Applications`, so the Dock and Cmd+Tab say CAO (`--no-app-copy` skips it).
- The installer supports macOS. On Linux it exits with code 4.
- The installer never changes your main window's app home or the core session CAO uses. `uninstall.sh` removes what the installer made and keeps the window's home as `<home>.removed-<epoch>`.

## How CAO uses the core

- **Labels.** Every terminal carries `cao.managed=1`, `cao.terminal_id`, `cao.session`, `cao.window`, and `cao.home`, plus the app's `herdr.space` and `herdr.tab` placement labels. Every terminal in one CAO session gets the same `herdr.space`, so the session's terminals share one space in the app.
- **Role labels.** A terminal that CAO creates for an agent also carries `cao.profile`, the agent profile name, and, for a worker that `assign` or `handoff` creates from a supervisor on the same node, `cao.caller`, the supervisor's terminal id. CAO writes both labels in the same `terminal.run` call as the labels above, and it writes `cao.caller` only when it has a caller. These terminals have no `cao.caller`:
  - A root terminal, for example one from `cao launch`.
  - A worker that a supervisor on another node places with `target_host`. That supervisor has no terminal on this node, so CAO records no caller. `GET /terminals/{terminal_id}` returns the supervisor's id as `callback_terminal_id` instead, until cao-server restarts.

  A plain window that `cao terminal restore` recreates has no agent, so it carries neither label, and neither do terminals created before CAO added them. For those, read `agent_profile` and `caller_id` from `GET /terminals/{terminal_id}`. The tmux and herdr TUI backends have no labels and ignore them.
- **`cao.waiting`.** A terminal that waits for workers carries `cao.waiting`, the number of its open delegation edges: one per `assign` with a callback (the default) and one per `send_message` that expects a reply or goes to a terminal the sender created, until a reply, an auto-forward or the worker's end closes it. CAO sets it when an edge opens or closes, removes it when the count is 0 (a terminal that waits on nothing carries no `cao.waiting`), and the delegation sweep, every 30 seconds, sets or removes it on every terminal of this server so that a missed update is corrected. One path closes edges without setting the label and leaves the correction to the sweep: the bulk deletion of terminal rows, which session teardown and the recycling of a flow's terminals use. Deleting one terminal, a worker that ends, and a worker that fails to start set it at once. A `handoff` sets nothing. Only the herdr-app backend writes it, and a caller cannot set it through `extra_labels`. A client that reads the labels, such as a Herdr window, can use it to tell an agent that waits for its workers from one that is done.
- **`cao.home`.** This label identifies the cao-server: the first 16 hex digits of the SHA-256 of its resolved CAO home directory (`CAO_HOME_DIR`). Several cao-servers with different homes can share one core. Each one sees, reconciles, and kills only terminals with its own `cao.home`. Two servers that use the same session name share that session's space in the app. Neither one removes the space while the other still has terminals in it.
- **Connections.** Each `hc connect` child is one connection to the core. A cao-server keeps one for requests, one for the event stream, and one for input to each CAO terminal it has typed into. So with N such terminals there are N + 2 children (about 2.4 MB of memory each with herdr-core 0.1.21). Input has a connection per terminal because herdr-core answers a connection one request at a time, and it can hold a send for up to 60 s while a person has a draft in that terminal. On a shared connection, that would hold input to every other terminal. A terminal's input connection closes when CAO kills the terminal, when the terminal is gone from the core, and when cao-server stops.
- **Tab order.** The app puts a newly adopted tab right after the tab that is selected, which is usually the conductor. After each adoption and each layout change, CAO puts its own tabs back in order: each tab follows the tab of the terminal that created it (`caller_id`), and siblings are in creation order. CAO moves only its own tabs, within the positions they already hold. A tab it doesn't own (your own tab in a CAO space, or another cao-server's) keeps its place, so two servers sharing a space don't undo each other. If you drag a CAO tab elsewhere, CAO moves it back on the next layout change.
- **Reconcile.** Each time the event stream connects, CAO compares its database with the core. It drops a CAO terminal row only when the core has a record for that terminal, under this server's `cao.home`, and every such record is finished. Rows the core has never heard of are left alone, including rows from the tmux or herdr TUI backend. It also kills live terminals that carry this server's labels but have no row.

## Behavior to know about

- **Kept terminals.** The app sets `keep` on terminals it shows. When an agent is lost, herdr-core replaces it with a shell, and CAO then closes that shell.
- **Native `error` is ignored.** herdr-core keeps a terminal's status at `error` after a failed turn, even once the agent is back at its prompt. CAO doesn't treat that as an error: it reads the agent's screen, as it does when no status is reported, so the terminal keeps taking input. Inbox messages aren't delivered on `error`; they go out on the next `idle` or `done`.
- **Inbox delivery waits about 10 seconds after a turn ends.** When an agent reports `idle` or `done`, CAO first waits for its output to settle (the same 10-second completion wait as on the other backends) and reports the terminal as processing meanwhile. So a queued message reaches it about 10 seconds after the turn ends, not at once.
- **Sends wait.** herdr-core waits until 3 s after a person's last key, and for their unfinished draft (up to 60 s). During its first 10 s, a new terminal also waits for its output to settle.
- **Enter comes 200 ms after the paste.** herdr-core presses Enter a fixed 200 ms after it pastes, and CAO can't ask it for a longer gap per send (only a herdr-core template can set one). Some agents need longer: CAO waits 2 s for Claude Code on tmux. So CAO checks each inbox delivery: if the agent hasn't started within 8 seconds and the message is still in its prompt box, CAO presses Enter again, up to two times. The first task sent to a new worker is checked in a similar way.
- **Keys without Enter.** Text sent without Enter (for example, a short menu answer) is typed key by key through `terminal.keys`. It's limited to 64 printable characters that `terminal.keys` accepts as key names: letters in either case, digits, and unshifted punctuation such as `-`, `/`, `.`, and `=`. A space is sent as the `space` key. Shifted punctuation such as `?`, `!`, `_`, or `:` is rejected, and nothing is typed.
- **History depth.** CAO reads at most 2000 lines of history per terminal. herdr-core 0.1.21 returns about 3000 screen rows, and a line wider than the terminal takes more than one row.
- **Stopping the core.** Remove the machine entry from the app first (`hc machine rm cao-local`, or the app's machine list), then stop the core with `HC_SESSION=cao hc server stop`. If you stop the core first, the app's reconnect can start it again.

## Migrating from the herdr TUI

If you used the herdr TUI for your own work, `scripts/herdr_tui_to_app.py` copies it into the app once. It reads the TUI (herdr 0.9.x) and creates a herdr-core terminal for every pane, placed so the app shows one space per TUI workspace and one tab per TUI tab. Claude Code and Codex panes are relaunched and resume their agent session with their original arguments. Every other pane gets a login shell in its last working directory. The script only reads the TUI and never changes it.

```bash
python3 scripts/herdr_tui_to_app.py                               # dry run: print the plan
python3 scripts/herdr_tui_to_app.py --tui-session default --apply # create the terminals
python3 scripts/herdr_tui_to_app.py --rename-only                 # name spaces and tabs later
```

- The target is your `default` hc session, the one the app opens. Use `--hc-session <name>` to change it.
- The TUI's `cao` session is skipped unless you pass `--include-cao`. CAO's own terminals don't move: after you switch `terminal.backend` to `herdr-app`, start new CAO sessions. The reconcile leaves the old herdr TUI rows in the database alone.
- Running it again is safe. Each terminal carries a `migrated.from` label, and the script skips panes it has already copied.
- Run `python3 scripts/herdr_tui_to_app.py --help` for every option. `--self-test` runs its offline checks.

## Troubleshooting

- List the CAO core's terminals: `HC_SESSION=cao hc ls` (`--all` includes finished ones).
- Check the core: `HC_SESSION=cao hc server status`.
- On the app's machine, list the machines the app knows: `hc machine ls`.
- If cao-server was started inside an app terminal, it ignores the inherited `HC_TERMINAL_ID`, `HC_TERMINAL_SOCKET`, `HC_SCOPE`, and `HC_SOCKET`, so it always talks to its own session's core. That includes `cao attach` and the browser terminal.
