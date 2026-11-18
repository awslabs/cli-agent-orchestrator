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

## How CAO uses the core

- **Labels.** Every terminal carries `cao.managed=1`, `cao.terminal_id`, `cao.session`, `cao.window`, and `cao.home`, plus the app's `herdr.space` and `herdr.tab` placement labels. Every terminal in one CAO session gets the same `herdr.space`, so the session's terminals share one space in the app.
- **`cao.home`.** This label identifies the cao-server: the first 16 hex digits of the SHA-256 of its resolved CAO home directory (`CAO_HOME_DIR`). Several cao-servers with different homes can share one core. Each one sees, reconciles, and kills only terminals with its own `cao.home`. Two servers that use the same session name share that session's space in the app. Neither one removes the space while the other still has terminals in it.
- **Connections.** Each `hc connect` child is one connection to the core. A cao-server keeps one for requests, one for the event stream, and one for input to each CAO terminal it has typed into. So with N such terminals there are N + 2 children (about 2.4 MB of memory each with herdr-core 0.1.21). Input has a connection per terminal because herdr-core answers a connection one request at a time, and it can hold a send for up to 60 s while a person has a draft in that terminal. On a shared connection, that would hold input to every other terminal. A terminal's input connection closes when CAO kills the terminal, when the terminal is gone from the core, and when cao-server stops.
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
