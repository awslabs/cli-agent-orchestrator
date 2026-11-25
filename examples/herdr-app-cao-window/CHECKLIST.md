# Manual test checklist for the CAO window

This is a test script. You do not need to know how the window is built. Follow the lines in order, copy the commands, compare what you see with the "You should see" text, and write PASS, FAIL, or not run in the results table.

You run it twice: once with CAO on a remote machine, and once with CAO and the app on one machine with `--local`. The results table has one column for each run.

## Before you start

**You need:**

- A Mac with the Herdr app 0.1.16 or later. Your normal Herdr window can stay open: the checklist never changes it.
- `hc` 0.1.21 or later and `jq` on the Mac. Check with `hc --version` and `jq --version`.
- A CAO host you can reach with `ssh` (remote run), with `uv`, a checkout of this repository, and `hc` 0.1.21 or later on it. The checklist starts its own scratch cao-server there (B0), on the `herdr-app` backend with no auth token, so it never stops or freezes the server your real agents use.
- `herdr-app` on the Mac. Check with `which herdr-app`. The Herdr app puts it, and `hc`, in `~/.local/bin` as links.
- A checkout of this repository on the Mac, and a terminal open in `examples/herdr-app-cao-window/`.
- A scratch directory on the CAO machine for test sessions: `mkdir -p /tmp/cao-window-check`.
- Agent profiles that work on that CAO. A provider you can run (the lines say `claude_code` where a native provider matters).

**Never do these things.** They are not part of this checklist, and one slip could change your real setup:

- Never use your main Herdr home (`~/.config/herdr-app`) as the window's home.
- Never use the core session `default` (your normal Herdr core) or `cao` (CAO's own core) for the window.
- Never run `hc machine rm` against your main core, and never remove the CAO machine from your main window during this checklist. The README describes that step. Do it after the checklist passes.
- Never run `HC_SESSION=<the CAO core> hc ...` on the Mac in the remote run. On the Mac it starts an empty local core with that name. Run it on the CAO host only.
- Never stop, freeze, or edit the profiles of your real cao-server for this checklist. Lines C23, C29, C34, and C35 do those things, and they act on the scratch server of B0. With the real server, skip them and write `not run: scratch server only`: B0 lists what still runs there. While a server is stopped or frozen, its agents' MCP calls and schedules fail.

**Where to run the Mac commands.** Run every Mac command from a terminal that is not inside a Herdr window (Terminal or iTerm). A terminal inside Herdr sets `HC_TERMINAL_ID`, `HC_TERMINAL_SOCKET`, `HC_SCOPE`, and `HC_SOCKET`, and they may point `hc` at your main core. Then `hc machine ls` in B3 could show your main machines, and the count in B9 could be your main core's. `install.sh` and `uninstall.sh` clear these variables themselves. The checklist's own `hc` calls use `chk_hc`, defined below, which clears them too. Whether `HC_SOCKET` wins over `HC_SESSION` when both are set is unverified, so do not rely on `HC_SESSION` alone.

**Scratch names.** The checklist installs a scratch window next to your real setup. Set these once in the terminal you use for the Mac commands, from `examples/herdr-app-cao-window/`:

```sh
export CHK_EX="$PWD"
export CHK_HOME="$HOME/.config/herdr-app-cao-check"
export CHK_CORE=caocheck
export CHK_LAUNCHER=herdr-cao-check
export CHK_HOST=build-box
export CHK_DIR=/tmp/cao-window-check
export CHK_PORT=9890
export CHK_CAO_CORE=caochk
chk_hc() { env -u HC_TERMINAL_ID -u HC_TERMINAL_SOCKET -u HC_SCOPE -u HC_SOCKET hc "$@"; }
chk_install() { sh "$CHK_EX/install.sh" --cao-ssh-host "$CHK_HOST" --home "$CHK_HOME" --core-session "$CHK_CORE" --launcher-name "$CHK_LAUNCHER" --cao-port "$CHK_PORT" --cao-core-session "$CHK_CAO_CORE" "$@"; }
```

For the `--local` run, define `chk_install` with `--local` in place of `--cao-ssh-host "$CHK_HOST"`. `CHK_PORT` and `CHK_CAO_CORE` name the scratch server's port and core session (B0). To use your real server instead, set them to `9889` and `cao`.

`CHK_CORE` must be a core session name you have never used. `uninstall.sh` stops the core named in the manifest, so a name that already holds other machines would lose them.

Replace `build-box` with the ssh name of your CAO host. For the `--local` run, `CHK_HOST` is not used, and `CHK_DIR` is a directory on the Mac.

**Text in this file.** The window draws a middle dot between the parts of the status bar and between the words of a tooltip, and an ellipsis character after a few words. This file writes them as ` . ` and `...`. Quoted text is what the window prints. Case matters.

### How to record results

The results table is under "Results", after these notes.

Write one of PASS, FAIL, or not run for every ID in the table, with a note for each FAIL. A line marked "Scratch server only" that you skipped because you use the real server gets `not run: scratch server only`. "UNVERIFIED" lines (IDs starting with `U`) are checks of Herdr behavior that nobody could run before this checklist. For those, write what you saw even when it passes: the "Report back" text under each line says what.

### How to stop and clean up

If you must stop in the middle:

1. Quit the scratch window with Cmd+Q.
2. Run `sh uninstall.sh --home "$CHK_HOME"` (Part D).
3. Delete the test sessions you made and stop the scratch server (D4).

Your main window and your main home need no cleanup, because the checklist never touches them.

### How to tell a refusal from a failure

Every message from the installer starts with `install: `. The exit code tells you what happened:

| Exit | Meaning |
|---|---|
| 0 | Done, or `up to date`. |
| 2, 3, 4, 5 | A refusal. The installer checked something and wrote nothing. Read the message. |
| 1 | A failure. A file or an `hc` step failed after some files were written. Fix the cause and run the installer again. |

Check the code with `echo $?` right after the command.

### Results

| ID | What | Remote (16.4) | Local (16.5) | Notes |
|---|---|---|---|---|
| A1 | Node suites on a dev machine | | | |
| A2 | The pytest wrapper | | | |
| A3 | The Linux installer refusal | | | |
| B1 | macOS run of `install.test.mjs` | | | |
| B2 | Install into a fresh scratch home | | | |
| B3 | What the installer wrote | | | |
| B4 | The launcher | | | |
| B5 | A second install changes nothing | | | |
| B6 | A customized layout is refused, then replaced with `--force` | | | |
| B7 | The refusal for a missing CAO location | | | |
| B8 | `herdr-app check` on each mod | | | |
| B9 | The baseline before you open the window | | | |
| B10 | The window's home is mode 700 | | | |
| C01 | The launcher opens a second Dock tile | | | |
| C02 | A repeat launch makes no second process and raises the window | | | |
| C03 | Your main window is unchanged and the CAO terminal count is the same | | | |
| C04 | Only CAO sessions show | | | |
| C05 | The two left blocks fold and keep their state over a restart | | | |
| C06 | Keyboard: Up, Down, Left, Right, Enter, and P in Sessions and Agents | | | |
| C07 | A session shows its whole tree of agents, however many, and the block scrolls | | | |
| C08 | A pin survives a restart | | | |
| C09 | Agent tabs with a pinned worker | | | |
| C10 | A blocked worker marks its session and counts in the bar, and the list keeps its order | | | |
| C11 | A replied conductor clears on focus | | | |
| C12 | The hand-off toast | | | |
| C13 | New session, Ask first | | | |
| C14 | New session, Auto-approve | | | |
| C15 | New session, YOLO | | | |
| C16 | Env rows show the command instead of launching | | | |
| C17 | Create a schedule disabled | | | |
| C18 | Enable a schedule | | | |
| C19 | Run a schedule now | | | |
| C20 | Edit a schedule's times | | | |
| C21 | A workflow's first run shows plan approval needed | | | |
| C22 | Draft with AI fills a form | | | |
| C23 | Stopping cao-server shows the unreachable state | | | |
| C24 | Ask first shows "Reading the profile..." and Launch is inert | | | |
| C25 | Closing the dialog during a launch is refused, and the new session is selected | | | |
| C26 | The Ask-first and YOLO confirmations lock the form | | | |
| C27 | A confirm card is dropped when the right sidebar's item changes | | | |
| C28 | Create and enable confirms by click and by Tab then Enter | | | |
| C29 | A failed profile read can be retried | | | |
| C30 | Launch refuses an empty or relative directory | | | |
| C31 | Edit of a Python workflow works with its hash | | | |
| C32 | A changed workflow gives the stale-hash reload | | | |
| C33 | A YAML name mismatch blocks Save | | | |
| C34 | After a lost Draft launch, Retry deletes the author session | | | |
| C35 | cao-server stopped with a dialog open | | | |
| C36 | No draft author session is left | | | |
| C37 | An allowed-tools override with `*` takes the YOLO dialog | | | |
| C38 | Create schedule on a server without `POST /flows/validate` | | | |
| C39 | A refused author profile shows its refusal | | | |
| C40 | Schedule limits | | | |
| C41 | Request bodies show in `ps`, env values never do | | | |
| C42 | Known gap note, best guess, and "stuck?" | | | |
| C43 | Workflow Cancel and Resume are confirmed | | | |
| C44 | Schedule... from a workflow, and "Scheduled by" | | | |
| C45 | A duplicate schedule name | | | |
| C46 | New session Advanced fields | | | |
| C47 | Sessions: one line per session | | | |
| C48 | The New session control | | | |
| C49 | Agents: the tree and its indentation | | | |
| C50 | The filter header and its clear control | | | |
| C51 | "Started by schedule X" opens the right sidebar at that schedule | | | |
| C52 | The top bar with one shown agent shows its one tab | | | |
| C53 | The Schedules and Workflows buttons | | | |
| C54 | The status bar parts are buttons | | | |
| C55 | The "?" help and its two notes | | | |
| C56 | The right sidebar: a list, its details and the back arrow | | | |
| C57 | "Show prompt" keeps the prompt collapsed | | | |
| C58 | The dialog: size, first read and a closing key | | | |
| C59 | Esc with edits, in each form and in a confirmation | | | |
| C60 | Create and enable, confirmed inside the dialog | | | |
| C61 | A draft closed mid-draft leaves no author session | | | |
| C62 | No label is clipped at the narrowest widths | | | |
| C63 | The dialog's open state: a lease | | | |
| C64 | Restart with the right sidebar left open | | | |
| C65 | An answer after leaving the right sidebar's details | | | |
| C66 | A workflow Run in the dialog: plan approval, the "not submitted" row, and nothing typed is stored | | | |
| C67 | The Agents block highlights the terminal you are in | | | |
| C68 | Mission Control and Cmd-` list the window as CAO | | | |
| C69 | The top-left of the sidebar shows the CAO mark and says CAO, with the search and fold buttons | | | |
| C70 | An agent that waits on the workers it assigned shows the waiting mark and "waiting on N workers", not done or Replied, until the reply arrives | | | |
| C71 | Cmd-Tab, the Dock and Mission Control name the window CAO, with the CAO icon; Herdr's own window still says Herdr | | | |
| U01 | Esc, Enter and Tab arrive in the dialog | | | |
| U02 | Textarea change events and `set_value` | | | |
| U03 | `randomUUID`, clipboard, and timers | | | |
| U04 | A click on the profile list inside the 200 ms blur window | | | |
| U05 | Layout resize and fold persistence | | | |
| U06 | Arrow and Enter key names | | | |
| U07 | The focused terminal's address form | | | |
| U08 | Toast overlay geometry and timeout unit | | | |
| U09 | `hc read` and `hc label` through the window's core | | | |
| U10 | The turn number increments on input | | | |
| U11 | The terminal list on a disconnect or while loading | | | |
| U12 | The Dock badge | | | |
| U13 | The toast icon | | | |
| U14 | Text fields created after the window starts | | | |
| U15 | A 16-row Textarea holding about 120 KiB | | | |
| U16 | The 8-hex draft suffix | | | |
| U17 | `GET /terminals/{id}/output?mode=last` for `claude_code` | | | |
| U18 | `POST /sessions` against the 20 s limit | | | |
| U19 | Two windows write one CAO structure document, and the updater | | | |
| U20 | The overlay's size at the smallest window | | | |
| U21 | Esc on a `dialog` overlay (H1) | | | |
| U22 | Esc on a `window` overlay (H2), only if U21 failed | | | |
| U23 | Tab order across the fields (H3) | | | |
| U24 | Show and hide of the right sidebar, the pane's reflow, and a restart | | | |
| U25 | `shared()` across the new blocks | | | |
| U26 | A press on a bar control does not drag or maximize the window | | | |
| U27 | A hidden sidebar copy receives the answer of a request started before it was hidden | | | |
| U28 | The theme key and folder of a copied settings file | | | |
| U29 | The left sidebar's edge drags between 200 and 640 px | | | |
| D1 | `uninstall.sh` runs | | | |
| D2 | Your main home is untouched | | | |
| D3 | The CAO terminal count is the same as before | | | |
| D4 | Cleanup of test data on the CAO machine | | | |
| D5 | Uninstall leaves a launcher it did not write | | | |
| D6 | Uninstall refuses a bad manifest | | | |

## Part A. Before the window

Run Part A on the Linux dev machine, or any machine with `node`, `jq`, and `uv`. No Herdr app is needed.

### A1 Node suites

From `examples/herdr-app-cao-window/`:

```sh
for f in $(find . -name '*.test.mjs' | sort); do env -u CAO_LOG_LEVEL node "$f" > /tmp/node-out.txt 2>&1; echo "$? $f $(grep -c '^ok - ' /tmp/node-out.txt) ok, last line: $(tail -1 /tmp/node-out.txt)"; done
```

- **You should see:** 25 lines. Each starts with `0`, and each ends with `all cases passed`. The `ok` counts when this checklist was written:

| File | ok |
|---|---|
| `install.test.mjs` | 201 |
| `launcher.test.mjs` | 16 |
| `lib/agents.test.mjs` | 110 |
| `lib/cao.test.mjs` | 47 |
| `lib/draft-followups.test.mjs` | 13 |
| `lib/draft-view.test.mjs` | 175 |
| `lib/draft.test.mjs` | 75 |
| `lib/flows-followups.test.mjs` | 5 |
| `lib/flows-form.test.mjs` | 96 |
| `lib/flows.test.mjs` | 154 |
| `lib/honesty.test.mjs` | 13 |
| `lib/launch-followups.test.mjs` | 6 |
| `lib/launch.test.mjs` | 249 |
| `lib/logic.test.mjs` | 289 |
| `lib/names.test.mjs` | 9 |
| `lib/shell.test.mjs` | 86 |
| `lib/store.test.mjs` | 18 |
| `lib/workflows-followups.test.mjs` | 3 |
| `lib/workflows-view.test.mjs` | 295 |
| `lib/workflows.test.mjs` | 88 |
| `make-window.test.mjs` | 31 |
| `portability.test.mjs` | 51 |
| `test/harness.test.mjs` | 11 |
| `test/security-wiring.test.mjs` | 23 |
| `test/shell-wiring.test.mjs` | 139 |

  That is 2203 cases in all. `portability.test.mjs` also prints `private names are not checked: set CAO_PORTABILITY_EXTRA_PATTERNS or CAO_PORTABILITY_EXTRA_PATTERNS_FILE`. That is expected.
- **Broken means:** a first column other than `0`, a `FAIL - ` line, or a `SKIP - ` line. A `SKIP` is not a pass.
- **Note:** the parity cases in `lib/launch.test.mjs` read two files from `src/cli_agent_orchestrator/utils/`. Run this from a full repository checkout, not from a copy of the example folder alone.

### A2 The pytest wrapper

From the repository root. Run one pytest at a time. On Linux you can put `flock /tmp/cao-window-pytest.lock` in front of the command so a second run waits for the first. That prefix is optional and Linux only: macOS has no `flock`.

```sh
env -u CAO_LOG_LEVEL uv run pytest --no-cov -p no:cacheprovider -q test/examples/test_herdr_app_cao_window_node.py
```

- **You should see:** a row of dots and `65 passed` (about 540 seconds on the dev machine this was written on, most of it `install.test.mjs`; pytest also prints a few warnings). 25 of the 65 are the node suites of A1; the rest check the wrapper itself.
- **Broken means:** any `failed` or `error`, or a `skipped` count above 0 (a skip here means `node` or `jq` was not found).
- **Note:** `CAO_LOG_LEVEL` must be unset for the CAO test suites. The command above does that with `env -u CAO_LOG_LEVEL`.

### A3 The Linux installer refusal

On a Linux machine with `hc` and `jq`, from `examples/herdr-app-cao-window/`:

```sh
sh install.sh --local --home /tmp/cao-window-linux-check --bin-dir /tmp/cao-window-linux-bin; echo "exit=$?"; ls /tmp/cao-window-linux-check /tmp/cao-window-linux-bin
```

- **You should see:** `install: the Herdr app on Linux is unsupported (see README, topology: Linux)`, then `exit=4`, then two "No such file" lines from `ls`.
- **Broken means:** any other exit code, or either folder exists afterwards. If the message is about `jq` or `hc` instead, install that tool and run again.

## Part B. Install into a scratch home

Run Part B on the Mac, from `examples/herdr-app-cao-window/`. Your main window can stay open.

### B0 Start a scratch CAO server (no pass/fail)

Lines C23, C34, and C35 stop or freeze cao-server, and C29 changes a profile in the agent store. While your real server is stopped or frozen, your real agents' MCP calls and schedules fail. So the checklist runs against a scratch server with its own home, its own port, and its own herdr-core session. The installer's `--cao-port` and `--cao-core-session` options (see `sh install.sh --help`) point the window at it, and `chk_install` passes both.

On the CAO machine (the Mac itself for the local run), in a terminal in the CAO repository, start it and leave it running:

```sh
export CAO_HOME_DIR=/tmp/cao-window-check-home CAO_API_PORT=9890 CAO_HERDR_APP_SESSION=caochk
mkdir -p /tmp/cao-window-check
if [ "$CAO_HOME_DIR" = /tmp/cao-window-check-home ] && [ "$CAO_HERDR_APP_SESSION" = caochk ]; then
  uv run cao-server --port 9890 --terminal herdr-app
else
  echo "STOP: not starting the server: these values are not the scratch ones"
fi
```

The values here are written out on purpose, and the launch line runs only when they are the scratch ones. If the check fails, the block prints `STOP: not starting the server: these values are not the scratch ones` and nothing starts. Never type `uv run cao-server ...` on its own, and never copy only its last line: a server started without the three exports uses your real `CAO_HOME_DIR` (`~/.aws/cli-agent-orchestrator`) and your real herdr-app session `cao`, and runs a second flow scheduler, inbox, and reconcile loop on your real terminals. Whenever this checklist says to start the scratch server again, run this whole block, in a terminal in the CAO repository on the CAO machine.

In a second terminal on the CAO machine, set the same variables for every `cao` and `curl` command you run later, and check the server:

```sh
export CHK_PORT=9890 CHK_CAO_CORE=caochk CAO_HOME_DIR=/tmp/cao-window-check-home CAO_API_PORT=9890
chk_hc() { env -u HC_TERMINAL_ID -u HC_TERMINAL_SOCKET -u HC_SCOPE -u HC_SOCKET hc "$@"; }
curl -s http://127.0.0.1:$CHK_PORT/health
```

`chk_hc` is the same helper as on the Mac. Every line below that says `chk_hc` on the CAO machine uses this copy.

- **You should see:** JSON in which `terminal_backend` is `herdr-app`. The scratch server keeps its database, profiles, schedules, and workflows under `CAO_HOME_DIR`, so your real ones stay out of reach. The core session `caochk` must be a name you have never used.
- **A throwaway session without model cost (optional):** `docs/mock-cli-provider.md` describes a `mock_cli` provider that is not on `PATH` outside pytest. From the repository root: `PATH="$PWD/test/providers/fixtures/bin:$PATH" cao launch --headless --agents developer --provider mock_cli --session-name chk-mock --working-directory /tmp/cao-window-check --auto-approve "hello"`. **Unconfirmed:** that document describes the tmux backend, and the mock was not run under the `herdr-app` backend, so the program may not be found there. It cannot produce Herdr's `blocked` or `replied` states either. Use a real `claude_code` throwaway session for those.
- **Real server instead:** set `CHK_PORT=9889` and `CHK_CAO_CORE=cao` and run `unset CAO_HOME_DIR CAO_API_PORT` in the terminals of the CAO machine.
  - **Skipped on the real server** (write `not run: scratch server only` for each): C23, C29, C34, C35, and the best-guess part of C42. They stop or freeze the server, or change its files, and nothing in them is safe to point at your real server. The scratch-core paragraph of D4 is skipped too.
  - **Still run against the real server:** the read-only lines (the counts of B9, C03, and D3, and every line that only looks at the window), and the lines that create things (`cao launch`, `cao install`, `cao schedule add`, and the forms). Every one of those creates its things under a `chk-` name, and the sessions are `cao-chk-...`. Run them only when no agent is working that you would mind disturbing.
  - **Cleanup:** D4 prints `STOP` and removes nothing on a real server. Remove what you made by hand with the list in D4.
  - `uninstall.sh` is safe either way: it acts only on the window's core `caocheck` and refuses a core named `cao` or `default`. U19 needs the real core (see its text).

### B1 macOS run of `install.test.mjs`

```sh
node install.test.mjs | tail -3
```

- **You should see:** the last line is `all cases passed`. The count of `ok - ` lines was 201 on Linux (`node install.test.mjs | grep -c '^ok - '`).
- **Broken means:** a `FAIL - ` line, or `SKIP - install.test.mjs: jq is not installed`. Report the first FAIL line and the output after it.

### B2 Install into a fresh scratch home

Start the scratch server first (B0). Remote run (16.4):

```sh
chk_install; echo "exit=$?"
```

Local run (16.5): define `chk_install` with `--local` (see "Scratch names") and run the same command.

- **You should see:**
  - Many `install: wrote ...` lines: `layouts/cao-window.json`, five mod folders (`cao-window-brand`, `cao-window-panel`, `cao-window-side`, `cao-window-status`, `cao-window-tabs`), `shared.json`, the launcher path, and `cao-window-install.json`.
  - `install: added machine 'cao' to core session 'caocheck'`.
  - If your main `settings.json` names a theme and `themes/<name>.json` exists in your main home: `install: wrote themes/<name>.json`. If the file does not exist: one `install: note:` line that says the theme was not found (harmless for a theme Herdr ships).
  - If your main home has no `settings.json`: `install: note: a fresh Herdr home turns telemetry on by default; pass --telemetry off to turn it off`.
  - `install: toast mod off, mod imports: copy`.
  - `install: done. Open the window with: <your home>/.local/bin/herdr-cao-check`.
  - `exit=0`.
- **Broken means:** a non-zero exit. A line `install: warning: ... layouts/main.json not found; using the copy in this example` is not a failure. It means your main Herdr home has no layout file there, and the installer used the copy in this folder.

### B3 What the installer wrote

```sh
ls "$CHK_HOME" "$CHK_HOME/mods"
cat "$CHK_HOME/shared.json"
jq 'del(.files)' "$CHK_HOME/cao-window-install.json"
jq '.files | length' "$CHK_HOME/cao-window-install.json"
HC_SESSION="$CHK_CORE" chk_hc machine ls
```

- **You should see:**
  - `ls`: `cao-window-install.json`, `layouts`, `mods`, `shared.json` (and `settings.json` when your main home has one, and `themes` when that `settings.json` names a theme your main home has), and in `mods`: `cao-window-brand`, `cao-window-panel`, `cao-window-side`, `cao-window-status`, `cao-window-tabs`. There is no `cao-window-toast` folder.
  - `shared.json`: `"layout": "cao-window"` and `"frame.welcomed": "finished"`.
  - The manifest: `home` is your `$CHK_HOME`, `core` is `caocheck`, `machine` is `cao`, `caoCore` is `caochk`, `caoSshHost` is your host (or `local` is `true`), `app` is `/Applications/Herdr.app`, and `props` holds `machine`, `caoPort` (9890), `talkableProfiles`, `hiddenRootProfiles`, `quietMinutes`, `holdMaxSeconds`, `draftProfile`, `draftProviders`, and `contextPercentRegex`.
  - The file count: 93 on a first install without a copied `settings.json`, 94 with one (95 when a theme file is copied too); three of them are the side mod's own drawings (`crown.svg`, `pin-filled.svg`, `pin-tilted.svg`), two the tabs mod's button icons (`flows.svg`, `workflows.svg`), and eighteen are the brand mod's folder (`mod.json`, `main.js`, the fourteen shared modules and the two drawings of the CAO mark, `cao-mark.svg` and `cao-mark-dark.svg`). A different number is only a note. A missing launcher or layout entry is a FAIL.
  - `hc machine ls` in the scratch core lists exactly one machine, `cao`. You ran it with `chk_hc`, so this is the window's core and not your main one.
- **Broken means:** a `cao-window-toast` folder (the switch is off by default), a machine other than `cao`, or the main home named in the manifest.

### B4 The launcher

```sh
cat ~/.local/bin/"$CHK_LAUNCHER"
```

- **You should see** four lines. The last is: `exec open -n -a /Applications/Herdr.app --env HERDR_APP_HOME=<your CHK_HOME> --env HC_SESSION=caocheck`.
- **Broken means:** a missing `HC_SESSION=caocheck`, or a home that is your main home.

### B5 A second install changes nothing

Run the same command as B2 again.

- **You should see:** no `wrote` lines, then `install: toast mod off, mod imports: copy` and `install: up to date`. `exit=0`. `ls -l "$CHK_HOME/layouts"` shows the same time as before.
- **Broken means:** any `wrote` line, `install: done` instead of `install: up to date`, or a new file ending in `.bak-<number>` under `$CHK_HOME`. This holds when B2 copied a `settings.json` or a theme too.

### B6 A customized layout is refused, then replaced with `--force`

```sh
echo ' ' >> "$CHK_HOME/layouts/cao-window.json"
chk_install; echo "exit=$?"
chk_install --force; echo "exit=$?"
ls "$CHK_HOME/layouts"
```

(Use `--local` instead of `--cao-ssh-host "$CHK_HOST"` in the local run.)

- **You should see:** the first run prints `install: <path>/layouts/cao-window.json was changed since the last install; nothing was written. Re-run with --force to keep it as a .bak file and replace it` and `exit=2`. The second prints `install: kept the old layouts/cao-window.json as layouts/cao-window.json.bak-<number>` and `install: wrote layouts/cao-window.json`, with `exit=0`. `ls` shows `cao-window.json` and one `.bak-` file.
- **Broken means:** the first run writes anything, or the second run does not keep a backup.

### B7 The refusal for a missing CAO location

```sh
sh install.sh --home "$CHK_HOME" --core-session "$CHK_CORE"; echo "exit=$?"
```

- **You should see:** the usage text, then `install: say where CAO runs: --cao-ssh-host HOST or --local`, then `exit=2`.
- **Broken means:** any file written. The refusals for core sessions `cao` and `default` and for your main home are covered by `install.test.mjs` (B1). The checklist does not try them by hand, because a bug there would touch your real setup.

### B8 `herdr-app check` on each mod

```sh
for m in cao-window-brand cao-window-panel cao-window-side cao-window-status cao-window-tabs; do HERDR_APP_HOME="$CHK_HOME" herdr-app check "$CHK_HOME/mods/$m"; echo "$m exit=$?"; done
```

- **You should see:** `exit=0` and no errors for each mod.
- **Broken means:** any error. **Unconfirmed:** the exact form of the `check` command was not run when this checklist was written. If it rejects the arguments, run `herdr-app check --help`, use the form it shows, and write that form in the Notes column.

### B9 The baseline before you open the window

Record these four things before C01. You compare them in C03 and D2.

1. The number of CAO terminals, in the scratch core. On the CAO host, in the terminal where you ran the second block of B0: `HC_SESSION=$CHK_CAO_CORE chk_hc ls`. In the local run, run the same command in the Mac terminal. Count the terminals it lists (one per line). It may be 0 on a new scratch server. Write the number N0 in the Notes column.
2. A look at your main window: the spaces it lists, and which tab has focus. Take a screenshot.
3. The files of your main home: `ls ~/.config/herdr-app | sort > /tmp/main-home-before.txt`.
4. The number of Herdr processes: `ps -ax -o pid,command | grep '[H]erdr.app/Contents/MacOS/Herdr'`. With your main window open you should see one line.

### B10 The window's home is mode 700

```sh
stat -f '%Lp' "$CHK_HOME"
```

- **You should see:** `700`. The installer sets the home to owner-only on every run, because the manifest names the launcher and the core session that `uninstall.sh` acts on.
- **Also check:** `chmod 755 "$CHK_HOME"`, run the B2 install again, and run `stat` again. You should see `up to date` from the installer and `700` from `stat`. The installer closes a home that was left open even when nothing else changes.
- **Broken means:** any other mode after an install.

## Part C. The window

Run Part C on the Mac, with the scratch window installed (B2). Each line has the same four parts: what to do, what you should see, what broken means, and which Herdr checks (U lines) it touches.

### C0. Prepare the CAO side

You need these on the CAO machine before the lines that say so.

- The scratch cao-server of B0, running, on the port you installed with (`$CHK_PORT`). Every `cao` and `curl` command below runs on the CAO machine with the variables of B0 set.
- A **supervisor session**: a root agent with two or more workers. If you have none, start one. This uses the built-in `code_supervisor` profile, which starts workers with `assign`:

  ```sh
  cao launch --headless --agents code_supervisor --session-name chk-sup --provider claude_code --working-directory /tmp/cao-window-check --auto-approve "Use assign to start two developer workers. Give each one a small task that takes a few minutes. Then wait for them."
  ```

  Use your own profiles where these do not fit. Any session with a root and two workers will do.
- An agent you can put into the **blocked** state, for C10: one that stops on a permission prompt. A recipe to try: launch a `claude_code` agent with `--allowed-tools fs_read` and ask it to run `ls`:

  ```sh
  cao launch --headless --agents developer --provider claude_code --session-name chk-block --allowed-tools fs_read --working-directory /tmp/cao-window-check --auto-approve "Run the shell command ls in the working directory and show me its output."
  ```

  For a **worker** that blocks, give the same instruction to a worker of the supervisor session below: tell the supervisor to assign it a task that needs a shell command. **Unconfirmed:** if Claude Code refuses the shell tool outright (CAO may pass it as denied) instead of asking, the agent never becomes `blocked`. Then try a task that asks for permission, such as writing a file outside the working directory, or write `not run` for C10.
- A **schedule** and **workflow** are created by lines C17 and C31, so you need none ahead of time.

### C01 The launcher opens a second Dock tile

1. Keep your main Herdr window open.
2. Run `~/.local/bin/herdr-cao-check` (the name is `$CHK_LAUNCHER`).
3. Wait a few seconds.

- **You should see:** a second tile in the Dock (named CAO when the installer made the app copy, C71; Herdr after `--no-app-copy`) and a new window with two blocks in the left sidebar, **Sessions** (with a `+`) above **Agents**, a top bar above the pane with the buttons **Schedules** and **Workflows** at its right, no right sidebar (it starts hidden), and a status bar at the bottom that starts with `cao`. `ps -ax -o pid,command | grep '[H]erdr.app/Contents/MacOS/Herdr'` now shows two lines.
- **Broken means:** no new window, a window that shows your own spaces or terminals, or an error box. If the window opens empty, see Troubleshooting in the README.
- **Optional:** `HERDR_APP_HOME="$CHK_HOME" herdr-app status | head -5` should show the layout `cao-window` and no errors (the output was not seen when this was written).

### C02 A repeat launch makes no second process and raises the window

1. Click your main window so it is in front.
2. Run `~/.local/bin/herdr-cao-check` again.

- **You should see:** the process count from `ps` stays at two, and the CAO window comes to the front.
- **Broken means:** a third process, or a second CAO window.
- **UNVERIFIED:** whether the window visibly comes to the front. Report whether it did. The repeat-launch behavior is the app's own, so it is checked here and not in the stub tests.

### C03 Your main window is unchanged and the CAO terminal count is the same

1. Look at your main window. Compare with the screenshot from B9.
2. Count the CAO terminals again, the way you did in B9 (`HC_SESSION=$CHK_CAO_CORE chk_hc ls` on the CAO machine, as in B9).

- **You should see:** the same spaces and focus in your main window, and the same number N0 of CAO terminals.
- **Broken means:** a changed main window, or a different count. Opening the window must never start, stop, or move a CAO terminal.

### C04 Only CAO sessions show

- **Do:** look at the **Sessions** block in the CAO window.
- **You should see:** only CAO sessions. Each name starts with `cao-`. None of your own Mac tabs or terminals appear anywhere in the window. With no CAO sessions the block reads `No CAO sessions`.
- **Broken means:** a session or terminal that is not CAO's.

### C05 The two left blocks fold and keep their state over a restart

1. Click the header row of **Agents** (the row with the arrow and the title). It folds to the header, and the summary reads the selected session's name or `none selected`.
2. Click it again. It unfolds.
3. Fold **Agents** once more. Quit the window with Cmd+Q, then run the launcher again.

- **You should see:** after the restart **Agents** is still folded with its summary, and **Sessions** is as you left it. Fold **Sessions** the same way: its summary is the number of sessions, such as `13`, or `13, 1 asking`. The fold of each block is independent.
- **Broken means:** both blocks fold together, a block comes back unfolded, or a block keeps a thin stripe of its old content.
- **UNVERIFIED:** U05 (layout resize and fold persistence).

### C06 Keyboard: Up, Down, Left, Right, Enter, and P in Sessions and Agents

1. Click an empty part of the **Sessions** block so it has the keyboard. (You need at least one session with a worker for the last step.)
2. Press **Down**: a highlight moves from the header to the first session line, and on to the next line for each press. **Up** moves back.
3. Press **Left** on a line: the highlight goes to the block header. Press **Left** on the header: the block folds. Press **Right** on the header: it unfolds.
4. Press **Enter** on the header: it folds or unfolds. Press **Enter** on a session line: the middle pane shows the session's first talkable agent, and the **Agents** block shows the session.
5. Click an empty part of the **Agents** block, move down to a worker row (indented, with a small push-pin at the right: tilted about 45 degrees, outline only, muted) and press **P**: the pin stands upright, is filled solid in the accent colour (not an outline with an accent stroke), keeps its size, the row does not move, and its tooltip reads `pinned`. Press **P** again: the pin is tilted, outline-only and muted again and the tooltip reads `click to pin`. **Enter** on an agent row puts that agent in the pane and leaves the selected session as it is.

- **You should see:** each step as written.
- **Broken means:** a key that does nothing. Write the key's name in the Notes column.
- **UNVERIFIED:** U06 (key names), U01 (the block receives the keys).

### C07 A session shows its whole tree of agents, however many, and the block scrolls

Use the supervisor session from C0. Select it in **Sessions**.

- **You should see:** in the **Agents** block, every agent of the session as a tree: the root agent, the workers it started one step in, the workers of those one step further, and so on, however many agents the session has. There is no `N workers` line, no `hide workers` line and no chevron: a session of 13 agents or of 40 shows all of them. When the tree is taller than the block, the block scrolls with the wheel or a drag, and the header and the rows above stay reachable by scrolling back. If no session of yours has more than 12 agents, write `many agents not seen`.
- **Broken means:** a worker missing from the list, an `N workers` or `hide workers` line, a chevron, agents drawn at one indent while their callers are listed, or a tree taller than the block that does not scroll.

### C08 A pin survives a restart

1. Click the pin of one worker in the **Agents** block. The pin decides the worker's top-bar tab. It becomes an upright pin filled in the accent colour and its tooltip reads `pinned`.
2. Every worker stays visible, pinned or not: a pin never hides or shows a row.
3. Quit with Cmd+Q and run the launcher again.

- **You should see:** the same worker is still pinned (its pin upright, filled, in the accent colour) and visible after the restart.
- **Broken means:** the pin is gone, or the wrong worker is pinned.

### C09 Agent tabs with a pinned worker

With the worker from C08 pinned and its session selected:

- **You should see:** two tabs at the left of the top bar above the middle pane: the root agent's name and the worker's name, each the name of the agent's Herdr tab (for example `conductor-774e`), the same name its row has in the **Agents** block, the conductor's included. No tab carries the word `worker` or any other tag. The tabs are in the order of the **Agents** block's rows: the conductor first, then each agent's children under it, so a pinned worker stands right after its parent and before the parent's later sibling, and pinning two workers in either order gives the same tabs. The main agent's tab has the crown after its name, and no other tab has it. Each tab has, before its name, the logo of the agent's program (the same logo as its row in the **Agents** block, smaller, as the tabs of a normal Herdr window show it), and no status mark (no spinner, ring, dot or `!` square): the state is drawn on the agent's row in the **Agents** block and on the session's line. Hover a tab: its tooltip is its row's, beginning with the agent's name and its state in words (for example `conductor-774e: done for 5 minutes`). The tab of the focused agent is highlighted. Click the worker's tab: the pane shows the worker. Click the root's tab: it shows the root.
- Unpin the worker (click its filled, accent-coloured pin in the **Agents** block, or **P** on that agent's row): the worker's tab disappears and the root agent's tab stays, and the top bar keeps its height and its two buttons.
- **Broken means:** no tab for a session whose only shown agent is its conductor, no second tab with a pinned worker, a tab that does not switch the pane, a tab with the word `worker` beside its name, a tab that draws a status mark, an agent with no name on its tab, or a top bar that changes height.
- **UNVERIFIED:** U07 (the highlight follows focus), U26 (a click on a tab still arrives).

### C10 A blocked worker marks its session and counts in the bar, and the list keeps its order

1. Get a worker into the blocked state (C0). If you could only block a root, check every item below that does not mention a worker.
2. Look at the **Sessions** block and the status bar.

- **You should see:**
  - The session with the blocked worker keeps its place in the list: the list is in order of age, the oldest session first (a session's age is its oldest terminal's), and it did not move when the worker became blocked.
  - Its line in **Sessions** carries the needs-you mark, an orange square with `!` (Herdr's compact form), and no word. If Herdr reports the kind of prompt, the line's tooltip reads `needs you: permission` (the kind after a colon); without a kind it reads `needs you`.
  - Select that session: in the **Agents** block the blocked worker is shown below its caller (in a session of any size), with the needs-you mark (an orange square with `!`) and no pill; the tooltip of its row says `needs you`, with the kind when Herdr gives one.
  - The status bar reads like `cao . 3 sessions . 1 asking . 1 working`. The `asking` part appears only while an agent is blocked.
  - Click `1 asking` in the bar: the list shows only that session. Click it again: the filter clears.
  - Answer the prompt in the agent's pane. The mark disappears, and the list has not moved.
- **Broken means:** a row that moves when a state changes, the needs-you mark is missing, a word such as `Asking` is drawn on the row, the bar shows 0 or no asking part, or the filter does not work. A Herdr `error` state must not count as asking.
- **UNVERIFIED:** U12 (the Dock badge shows the asking count, see below).

### C11 A replied conductor clears on focus

1. Click a session so its conductor is in the pane. Send the conductor a message that takes a short while to answer.
2. Straight away click a different session, so the conductor is not focused.
3. Wait until the conductor finishes.

- **You should see:** the conductor's session shows the done dot in a different colour from a plain done dot (green, the theme's `ansi2`; compare it with a seen conductor's dot and with the selected row's fill) and no word, its tooltip reads `<session>: replied`, and the session does not move in the list. The bar shows `1 replied`.
4. Click the conductor's session line, so its pane is focused.

- **You should see:** within a second or two the green dot becomes the plain done dot and the session leaves the replied group. The **Agents** block shows `seen: seen` for the conductor.
- **Also check:** an agent that finished before you ever focused it in this window, and has no `herdr.seen` label, shows no green dot. Its line in the **Agents** block shows `seen: unknown`, and the bar shows `0 replied (1 unknown)`. That is by design: the window does not guess.
- **Broken means:** the green dot stays after you focus the agent, or a never-seen agent shows it.
- **UNVERIFIED:** U07, U09 (the label write), U10.

### C12 The hand-off toast

What ships: the window does not hold toasts. The hold-until-hand-off and Later logic exists only as tested code, because the 30-minute experiment that decides it has not run. This line checks what does ship.

**Part 1: the default install (no toast mod).**

1. With another session's agent about to need you, work in a different session. Make an agent become blocked (C10).

- **You should see:** Herdr's own toast at once (its text is Herdr's), and a number on the CAO window's Dock tile equal to the asking count (`1`). When no agent is blocked, no badge.
- **Broken means:** no toast at all, or a badge on the wrong Dock tile.
- **UNVERIFIED:** U12.

**Part 2: with the toast mod (optional).** Install again with the switch on, then restart the window:

```sh
TOAST_MOD=1 chk_install
```

The summary line must read `install: toast mod on, mod imports: copy`. Make another agent blocked.

- **You should see:** a toast drawn by the window: a rounded box with a bold title, a smaller body line, and a small `x` at the right. It appears at once, with no holding. Click its body: the agent's pane takes focus and the toast closes. Click the `x`: it closes. Left alone it disappears after about 6 seconds.
- **Not built, mark n/a:** "the toast shows a held ask" and "Later re-holds it". Write `n/a: not built` in the Notes column for those two. The Open and Later buttons do not appear.
- Run the install again without `TOAST_MOD=1` to turn it off.
- **Broken means:** a toast cut off at the window edge, a toast that never closes, or a click that does not focus the agent.
- **UNVERIFIED:** U08 (geometry and timeout unit), U13 (the icon).

### C13 New session, Ask first

1. Click the `+` in the **Sessions** header. The dialog opens with **New session** and the note `Global: not tied to the open session`.
2. In **Agent profile** click the field and pick a profile from the list. Under the field it reads `Selected: <name>`.
3. In **Working directory** type `/tmp/cao-window-check` (a path on the CAO machine).
4. In **Session name** type `chk-ask` (letters, digits, `-`, and `_` only). In **Message** type `Say hello and stop.`
5. Leave **Tool policy** on **Ask first**. Check that the **Equivalent command** box shows `cao launch --headless --agents <profile> --session-name chk-ask --working-directory /tmp/cao-window-check 'Say hello and stop.'`.
6. Press **Launch**.

- **You should see:** a confirmation titled `Launch <profile> on <provider>?` that replaces the form body inside the dialog (the form's buttons stay at the bottom, dimmed). It lists `Declared role: ...`, `Declared tools: ...` with the small label `declared by the profile; the server resolves the final set`, and `Enforcement: ...`. On a provider that does not enforce tool limits (a prompt-only or no-enforcement provider) with a restricted profile it adds `not enforced by this provider`. **Cancel** has the focus.
- Press **Tab**, then **Enter** (or click **Launch** in the confirmation).
- **You should see:** the button reads `Starting...`, then the new session is selected behind the dialog, and a card in the dialog reads `Started cao-chk-ask.` (the server adds the `cao-` prefix), `Allowed tools recorded by the server: ...`, and `The first message was delivered.` A little later the card line changes to `The agent has replied.` The session appears in the Sessions block. Press **Close** on the card.
- **Broken means:** no confirmation for Ask first, a session started before you confirmed, or no card.
- **UNVERIFIED:** U03, U04, U01.

### C14 New session, Auto-approve

1. Click `+` in Sessions. Fill the profile, working directory, and message as in C13, but type `chk-auto` as the session name.
2. Choose **Auto-approve**.
3. Press **Launch**.

- **You should see:** no confirmation. The button reads `Starting...` and the session starts with the same kind of card, `Started cao-chk-auto.` Press **Close** on the card.
- **Broken means:** a confirmation appears, or a second session starts.

### C15 New session, YOLO

1. Click `+`. Fill the profile, working directory, and message. Choose **YOLO** (shown in red). Leave **Session name** empty.
2. Press **Launch**.

- **You should see:** no confirmation. The **Session name** field shows `a session name is needed to confirm YOLO` and has the keys.
3. Type `chk-yolo` in **Session name** and press **Launch**.

- **You should see:** a red confirmation titled `Launch <profile> with every tool allowed?` (on `kiro_cli` or `opencode_cli` it reads `Launch <profile> on <provider> with --yolo?` and adds the line `native at install time (the installed agent's policy applies; launch overrides do not change it)`). It says `Type the session name to confirm: chk-yolo` and has a text field with the keys. The **Launch with YOLO** button is dimmed.
4. Type a different name: the button stays dimmed. Type `chk-yolo`: it enables. Confirm.

- **You should see:** a card with `Allowed tools recorded by the server: ...`, which for YOLO is usually `*`. Press **Close** on the card.
5. Click `+` again.

- **You should see:** the form is empty and the policy is back on **Ask first**. YOLO is never remembered.
- **Broken means:** YOLO works without the typed name, or the policy stays on YOLO.

### C16 Env rows show the command instead of launching

1. Click `+`. Fill the profile and working directory. Click **Advanced**, then **Add variable**. Type `API_URL` as the name and `https://example.test` as the value.

- **You should see:** the **Launch** button is dimmed, with the note `env values are not sent from this window` beside it. Under the env rows a red line reads `env values are not sent from this window: run the command below in a terminal on the CAO machine instead.` The **Equivalent command** shows `--env API_URL=...` with the value masked. A **Copy with values** button appears.
2. Press **Launch** anyway.

- **You should see:** nothing is sent. Check on the CAO machine: no new session.
3. Click **Copy with values**. The note reads `Copied, values included`. Paste into a text editor.

- **You should see:** the pasted command holds `--env API_URL=https://example.test`. **Copy** alone copies the masked form and reads `Copied, values masked`.
4. Change the name to `LD_PRELOAD`.

- **You should see:** the row says `key is read by the loader, shell or an interpreter when the pane starts and cannot be forwarded`. A name `CLAUDE_CONFIG` says `key uses a prefix reserved for provider env (CLAUDE, CODEX_, __MISE_)`. `CLAUDE_CODE_USE_BEDROCK` shows no row error.
- **Broken means:** a request is sent with env values, or the command shows the value in the masked view.
- **UNVERIFIED:** U03 (clipboard), U14.

### C17 Create a schedule disabled

1. Click `Schedules` in the top bar, then the `+` in the list's header. The dialog shows **New schedule**, a box **Draft with AI** with the label `proposed`, and the hint `Creates the schedule disabled: enable it on purpose from its details.`
2. **Name:** `chk-flow`. **What it runs:** `An agent prompt`.
3. **Schedule:** type `0 9 * * 1-5`.

- **You should see:** a red note: `This server counts days of the week from Monday as 0, so 1-5 is Tuesday to Saturday. Use names such as mon-fri; the next runs below show what it will do.` Under **Next 3 runs** (`from the server's scheduler`) three lines such as `Tue 2026-11-24 09:00`, and one of the three is a Saturday (`Sat`) when the window is long enough to reach one. (If the three runs do not include a Saturday, it is not a failure.)
4. Click the preset **Weekdays 09:00**.

- **You should see:** the schedule becomes `0 9 * * mon-fri`, the note disappears, and the next runs show three weekdays.
5. Pick an **Agent profile**, leave **Provider** on its default, and type `Say hello.` in **Prompt**. Press **Create schedule**.

- **You should see:** the dialog closes and the right sidebar opens (if it was closed) on the details of the new schedule, which show `Disabled` and `None. The schedule is disabled.` Click the back arrow: its row in the schedules list reads `off`.
6. On the CAO machine: `curl -s http://127.0.0.1:$CHK_PORT/flows/chk-flow | jq .enabled` prints `false`.
- **Broken means:** the schedule is created enabled. (On a server without `POST /flows/validate` Create is disabled instead: see C38. If a server still answers enabled, the window warns: `This server ignored enabled: false and created the schedule enabled. Use the switch to disable it if you did not mean that.`)

### C18 Enable a schedule

1. Click `Schedules`, then the row `chk-flow`. In its details click the switch (left of the word `Disabled`).

- **You should see:** a confirm card `Enable schedule chk-flow?` over the details in the right sidebar with `This schedule launches an agent with nobody watching, and a schedule's script runs a local file when one is set.` The focus is on **Cancel**.
2. Press **Enter** without moving.

- **You should see:** the card closes and nothing is sent. The schedule stays disabled.
3. Click the switch again, press **Tab**, press **Enter**.

- **You should see:** `enabling...`, then `Enabled.` Its row in the schedules list now reads `next <date> <time>`. The details in the right sidebar show `Enabled` and three next runs.
4. `curl -s http://127.0.0.1:$CHK_PORT/flows/chk-flow | jq .enabled` prints `true`.
- **Broken means:** Enter on the first card enables the schedule, or a failed enable moves the switch.

### C19 Run a schedule now

1. Open the details of `chk-flow` (enabled) in the right sidebar. Click **Run now**.

- **You should see:** a confirm card `Run schedule chk-flow now?` with the same warning text, focus on **Cancel**. Confirm with the **Run now** button.
- **You should see:** `running...` in the details and a `running` state on the schedule's row in the list. It ends with `The schedule ran.` (or `The run was skipped (its script said no, or its session was still working).`). This can take a minute. A session `cao-flow-chk-flow` appears in Sessions. The schedule's **Runs** list shows that session; click it. The session is selected in **Sessions** and the right sidebar stays as it is.
2. In the **Agents** block of that session:

- **You should see:** `Started by schedule chk-flow` with the small label `inferred from the session name`, and `schedule: weekdays at 09:00`.
3. Open the schedule's details again and use the switch to **disable** it. The card is `Disable schedule chk-flow?` with the focus on **Disable**. Confirm. The state reads `Disabled.`
- **Broken means:** a run without a confirm, or no session for the schedule.

### C20 Edit a schedule's times

1. Open the details of `chk-flow` in the right sidebar. Click **Edit**. The dialog shows **Edit chk-flow**. There is no name field.
2. Click the preset **Every day 09:00**. Press **Save changes**.

- **You should see:** the dialog closes and the right sidebar shows the details of `chk-flow`: `daily at 09:00, 0 9 * * *`. `curl -s http://127.0.0.1:$CHK_PORT/flows/chk-flow | jq -r .schedule` prints `0 9 * * *`.
- **Broken means:** the schedule does not change, or other fields (the prompt, the profile) changed.
- **Note:** a schedule whose file is outside `CAO_HOME_DIR/flows` cannot be edited: the save answers 409. The schedule made in C17 is inside it.

### C21 A workflow's first run shows plan approval needed

You need a workflow that was never approved. Use the one from C31 (`chk-wf-py`), or create it first. Do not approve it in a terminal unless you want a real agent run.

1. Click `Workflows` in the top bar and open the workflow in the right sidebar (click its row). Click **Run**. A dialog **Run chk-wf-py** opens. If the spec does not describe inputs it shows `inputs not described by the spec` and an empty name/value row. Leave them empty. Press **Submit**.

- **You should see:** a box with the headline `Plan approval needed. Approve it in a terminal`, `Plan id: <the id>`, the command `cao workflow approve <the id>` with a **Copy** button, and the button `I approved it: submit again`. In the right sidebar's details of the workflow, the **Runs** list holds a row `not submitted` with `approval needed` (C66). There is no button that approves the plan.
- **Broken means:** an approve button exists, or the window shows the `CAO requires auth` message. If the server starts the run instead of asking for approval, cancel it in **Runs**, write what happened in the Notes column, and mark the line FAIL.
- **Optional:** run the printed command in a terminal on the CAO machine, then press `I approved it: submit again`. A `Run started: <run id>` line appears. This starts real agents. Cancel the run afterwards in **Runs**.

### C22 Draft with AI fills a form

Draft needs an author profile that meets three rules (README, "Draft with AI needs an author profile"): it declares exactly one MCP server, `cao-mcp-server`; it sets no `native_agent`; and its provider is native, not install-time, listed in `draftProviders`, and installed on the CAO machine. Make one. On the CAO machine save this as `chk-draft-author.md`:

```yaml
---
name: chk-draft-author
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

Install it with `cao install ./chk-draft-author.md`. Then install the window again with the profile named:

```sh
chk_install --prop 'draftProfile="chk-draft-author"'
```

Quit and reopen the window. Launch one session from **New session** first (C14), so the window knows a working directory.

1. Click `Schedules`, then `+`. In the **Draft with AI** box type: `A schedule named chk-draft that runs on weekdays at 9am and asks an agent to say hello.` Press **Draft**.

- **You should see:** the box reads `drafting...` followed by the session name `cao-draft-flow-<8 characters>`, with a **Cancel** button. In the Sessions list that session carries a `draft` tag, and the status bar counts do not change for it.
2. Wait 30 to 90 seconds.

- **You should see:** the form fields fill in, each with the label `AI` beside its heading. The box reads `The form is filled. Check it before you create.` The `cao-draft-flow-...` session disappears from Sessions. Nothing is created: the Schedules list is unchanged until you press **Create schedule**.
- **Without** `draftProfile` set, **Draft** shows `Set draftProfile in the layout props to the profile the author should run as (install.sh --prop 'draftProfile="<profile>"').` and sends nothing.
- **Broken means:** the draft saves a schedule, the author session stays, or the fields have no `AI` label.
- **Profile check on a real CAO host.** Run both commands on the CAO machine, before or after the draft:

  ```sh
  curl -s http://127.0.0.1:$CHK_PORT/agents/profiles/chk-draft-author | jq '{mcpServers: ((.mcpServers // {}) | keys), native_agent: .native_agent}'
  CAO_AGENT_PLUGINS_ENABLED=1 cao plugin list
  ```

  You should see `"mcpServers": ["cao-mcp-server"]` (exactly that one entry), `"native_agent": null`, and `No agent plugins installed`. Use your own profile name in place of `chk-draft-author`. The window cannot see an Agent Plugin's MCP servers, so this check is yours to make. `cao plugin list` prints only each plugin's name, version, and skills, not its MCP servers. If it lists a plugin, look for a file named `mcp.json` in the plugin folder of the server you are using. On the scratch server that is `$CAO_HOME_DIR/agent-plugins/`, the same home `cao plugin list` reads (`find "$CAO_HOME_DIR/agent-plugins" -name mcp.json` should print nothing). On the real server it is `~/.aws/cli-agent-orchestrator/agent-plugins/`, or the `agent-plugins` folder under the `CAO_HOME_DIR` your real server runs with. Check the real home as well when the real server is the one the window uses, and write down every plugin you find in either place; `cao plugin validate <plugin folder>` prints `mcp.json: present` for one that has it. **Unconfirmed:** the layout under `agent-plugins/`, and that `find` finds the file, were read from the code and not run. The `cao plugin` group is hidden and refuses to run without `CAO_AGENT_PLUGINS_ENABLED=1`.
- **UNVERIFIED:** U16, U17.

### C23 Stopping cao-server shows the unreachable state

**Scratch server only.** If you are using the real server, write `not run: scratch server only` in the Notes column and skip this line.

1. Stop the scratch cao-server of B0 (Ctrl+C in its terminal).
2. Wait up to a minute.

- **You should see:** the **Sessions** header shows `CAO unreachable, retrying`. Open `Schedules` and `Workflows`: each list in the right sidebar shows the same line, and, under it, `Showing the last list, <N> seconds old` (or minutes). Their rows stay, and the counts on the two buttons keep their last values. The status bar gets a last part `CAO unreachable`, and its counts keep updating from Herdr data. The `+` controls still open the dialog, but pressing a write shows `CAO unreachable, retrying`.
3. Start the scratch server again: run the whole start block of B0 (the three exports, the `mkdir`, and the guarded launch), in a terminal in the CAO repository on the CAO machine. A fresh terminal has none of B0's variables, so do not type the `uv run` line alone:

```sh
export CAO_HOME_DIR=/tmp/cao-window-check-home CAO_API_PORT=9890 CAO_HERDR_APP_SESSION=caochk
mkdir -p /tmp/cao-window-check
if [ "$CAO_HOME_DIR" = /tmp/cao-window-check-home ] && [ "$CAO_HERDR_APP_SESSION" = caochk ]; then
  uv run cao-server --port 9890 --terminal herdr-app
else
  echo "STOP: not starting the server: these values are not the scratch ones"
fi
```


- **You should see:** the notes disappear within about a minute.
- **Broken means:** the window goes blank, or the lists are emptied.

### C24 Ask first shows "Reading the profile..." and Launch is inert

1. In New session fill the profile and working directory, and type `chk-read` as the session name. Keep **Ask first**.
2. Press **Launch** and press it again at once (double-click it).

- **You should see:** the button text changes to `Reading the profile...` for a moment, and only one dialog opens. The read takes a fraction of a second on a fast link, so you may not see the text.
- **If the form changes during the read:** press **Launch**, then at once type into **Message**. When the read returns, no dialog opens, and the form says `The form changed, press Launch again.`
- **Broken means:** two dialogs, or two sessions.
- If you see neither text, write `not seen` and move on.

### C25 Closing the dialog during a launch is refused, and the new session is selected

1. Fill a New session form with the session name `chk-discard` and **Auto-approve**. Press **Launch**: the button reads `Starting...`.
2. At once press Esc or click **Close**.

- **You should see:** the dialog stays open and says `A request is in progress. Wait for the answer.` It does not offer to discard the form while the request is out.
3. Wait for the answer.

- **You should see:** after a moment the card `Started cao-chk-discard.` shows in the dialog and the new session is selected in **Sessions** and shown in **Agents**. Press **Close** on the card.
- **Broken means:** the dialog closes while the request is out, the session starts but is not selected, or the card is missing. If the launch finishes before you can press Esc, write `too fast` and repeat with a slower provider.

### C26 The Ask-first and YOLO confirmations lock the form

1. Open the Ask-first confirmation (C13, step 6). Look at the dialog.

- **You should see:** only the confirmation in the dialog body, and the form's buttons dimmed at the bottom. The form fields are not drawn, so nothing behind it can be changed.
2. Press **Esc**. The confirmation closes, the dialog stays open and the form returns with every value kept.
3. Choose **YOLO**, set a session name, press **Launch**. The YOLO confirmation also shows no form behind it. Press **Esc**: the form returns with every value kept.
- **Broken means:** form fields stay visible and editable under a confirmation, or Esc closes the whole dialog.
- **UNVERIFIED:** U01 (Esc reaches the dialog).

### C27 A confirm card is dropped when the right sidebar's item changes

1. Create a second schedule (repeat C17 with the name `chk-flow2`), or use any other schedule.
2. Click `Schedules`, open `chk-flow` in the right sidebar and click **Run now**. The confirm card opens over its details.
3. Without answering, click the back arrow, then the row `chk-flow2`.

- **You should see:** the card is gone and the right sidebar shows `chk-flow2` with no card. Nothing was sent.
- **Check:** `chk-flow` was not run: no `cao-flow-chk-flow` session starts, and nothing happens when you press **Enter**. Open `chk-flow` in the right sidebar again: no card is open.
- **Broken means:** the card stays or shows over the other schedule, or **Enter** runs the first schedule.

### C28 Create and enable confirms by click and by Tab then Enter

1. Click `Schedules`, then `+`. Fill a new schedule named `chk-flow3` (schedule `0 9 * * mon-fri`, a profile, a prompt). Press **Create and enable...**.

- **You should see:** the form body is replaced by `Create schedule chk-flow3 and enable it?` with the unattended-agent warning, focus on **Cancel**.
2. Click **Create and enable**.

- **You should see:** the schedule is created, the dialog closes, and the right sidebar shows its details as enabled; in the schedules list its row reads `next <date> <time>`.
3. Repeat with `chk-flow4`, but press **Tab**, then **Enter**.

- **You should see:** the same result.
- **Broken means:** a schedule is created without a confirmation, or **Tab** then **Enter** does nothing.
- Disable `chk-flow3` and `chk-flow4` right now (open each in the right sidebar, use the switch, confirm): they run an agent unattended on weekdays at 09:00 while enabled. Remove them in D4.

### C29 A failed profile read can be retried

**Scratch server only.** If you are using the real server, write `not run: scratch server only` in the Notes column and skip this line. (On the real server `CAO_HOME_DIR` is unset, so the `mv` paths below would point at `/agent-store/`.)

This uses a throwaway copy of a profile, so no real profile moves. The scratch server's agent store is `$CAO_HOME_DIR/agent-store/`, which is `/tmp/cao-window-check-home/agent-store/`.

1. On the CAO machine save this as `chk-copy.md` and install it with `cao install ./chk-copy.md`:

   ```yaml
   ---
   name: chk-copy
   description: A throwaway profile for the retry check
   provider: claude_code
   role: reviewer
   ---
   ```
2. Open **New session** and wait until the profile list loads (it lists `chk-copy`). On the CAO machine, move only the copy away: `mv "$CAO_HOME_DIR/agent-store/chk-copy.md" "$CAO_HOME_DIR/agent-store/chk-copy.md.off"`.
3. In the form choose `chk-copy`, fill the working directory, keep **Ask first**, and press **Launch**.

- **You should see:** no dialog, and a red line with the server's words (or `could not read the profile`).
4. Move the copy back: `mv "$CAO_HOME_DIR/agent-store/chk-copy.md.off" "$CAO_HOME_DIR/agent-store/chk-copy.md"`. Press **Launch** again.

- **You should see:** the dialog opens. The window asks the server again, because it does not keep a failed read.
- **Broken means:** the error repeats after the file is back.
- **Unconfirmed:** that `cao install` puts the file at `$CAO_HOME_DIR/agent-store/chk-copy.md`. The path comes from the CAO docs and was not run. If `ls "$CAO_HOME_DIR/agent-store"` shows another place, move the copy there.
- **Also:** a profile named `search`, `templates`, or `schema` can never be read this way, because the server declares fixed routes with those names before the `{name}` route. If you have one, the Ask-first dialog does not open and the form says the profile could not be read. Auto-approve and YOLO still work.

### C30 Launch refuses an empty or relative directory

1. Open **New session**. Choose a profile. Clear **Working directory** (after an earlier launch it holds the last directory). Press **Launch**.

- **You should see:** no request, and under the field `a directory on the CAO machine is needed`. The field takes the keys.
2. Type `x/y` and press **Launch**.

- **You should see:** `the directory must be an absolute path on the CAO machine, starting with /`.
- On a first launch from the window the field is empty; after a launch it holds the last directory.
- **Broken means:** a session starts.

### C31 Edit of a Python workflow works with its hash

1. Click `Workflows`, then `+`. In the dialog, under **Template** click `Python script, two steps`. The note reads `The profile names in a template are placeholders: replace them with profiles installed on the CAO machine before you save.` Set **Name** to `chk-wf-py`.
2. Press **Validate**.

- **You should see:** `No findings.` or `No blocking findings. Warnings do not stop you from saving.` The size line reads `Size: <N> KiB of 120 KiB`.
3. Press **Save workflow**.

- **You should see:** the dialog closes and the right sidebar shows the details of `chk-wf-py`, with `Mode` and `Steps`.
4. Click **Edit** in those details. The dialog shows `Reading the workflow...`, then **Edit chk-wf-py** with **Tier** `python` and the source. Change the text of one prompt. **Save changes** is disabled until you press **Validate** and it is clean. Press **Validate**, then **Save changes**.

- **You should see:** the dialog closes and the right sidebar shows the details. On the CAO machine, `curl -s http://127.0.0.1:$CHK_PORT/workflows/chk-wf-py | jq -r .source` shows your change.
- **Broken means:** **Save changes** stays disabled after a clean validate, or a banner shows an error.

### C32 A changed workflow gives the stale-hash reload

1. In the right sidebar open the details of `chk-wf-py` and click **Edit**. Do not save yet.
2. On the CAO machine, change the file behind its back:

   ```sh
   p=$(curl -s http://127.0.0.1:$CHK_PORT/workflows/chk-wf-py | jq -r '.path // empty')
   if [ -n "$p" ] && [ -f "$p" ]; then printf '\n# touched\n' >> "$p"; else echo "STOP: no workflow file found for chk-wf-py (path: '$p')"; fi
   ```

   The `if` appends only to an existing file. Without it, a missing workflow would make `jq` print `null`, and `>> null` would create a stray file named `null` in the current directory. If you see `STOP`, check that `chk-wf-py` exists (C31) and that `CHK_PORT` is set.
3. In the window change a word, press **Validate**, then **Save changes**.

- **You should see:** a red line `This workflow changed elsewhere. Your text is kept; reload to see the new version.` and a **Reload** button. Click **Reload**: a confirmation `Replace your text with the server's version?` (`The workflow changed elsewhere. Your edits in this editor are lost.`) with **Keep my text** and **Replace my text**. Choose **Replace my text**: the editor now shows the server's version, ending in `# touched`.
- **Broken means:** the save succeeds and overwrites the server's change, or your text is lost without the dialog.

### C33 A YAML name mismatch blocks Save

1. Click `Workflows`, then `+`. Click the template `Sequential YAML spec, two steps`. **Name** fills with `hello-review-yaml`. Change **Name** to `chk-wf-yaml`.

- **You should see:** a red line: `The spec's name: line says "hello-review-yaml", but the workflow is named "chk-wf-yaml". The server refuses a spec whose name differs; make them the same.` **Save workflow** is disabled even after a clean **Validate**, and **Validate** still works.
2. Edit the `name:` line in the source to `chk-wf-yaml`: the red line goes away.
3. Type `runs` as the name.

- **You should see:** `"runs" is a name the server uses for its own routes. Pick another.`
4. Type a name of 65 letters (for example `a` repeated 65 times).

- **You should see:** `A workflow name is at most 64 characters.`
- **Broken means:** Save works with a mismatched name, or with a `runs` or 65-character name.

### C34 After a lost Draft launch, Retry deletes the author session (advanced)

**Scratch server only.** If you are using the real server, write `not run: scratch server only` in the Notes column and skip this line.

This check needs `draftProfile` (C22). It freezes the scratch cao-server for about 30 seconds.

The window reads the author profile before it launches, and keeps that answer while the form stays open. So do one good draft first, in the same form, and freeze the server after it.

1. In **New schedule**, complete one Draft as in C22. The box offers **Draft again**.
2. Find the scratch server's process id on the CAO machine and freeze it: `pid=$(lsof -ti tcp:9890 -sTCP:LISTEN); echo "$pid"; ps -p "$pid" -o args=`. The `ps` line must show `cao-server --port 9890`. Only then run `kill -STOP $pid`. The port is written out on purpose: never freeze a server on port 9889. If `lsof` is missing, find the `cao-server --port 9890` process with `ps -ax` and use its id.
3. Press **Draft again**.
4. Wait 20 seconds.

- **You should see:** the box reads `The author launch failed: CAO did not answer. The session cao-draft-flow-<8 characters> may have started. Closing this form deletes the session, or you can delete it by hand.` with **Retry**.
5. Unfreeze the server: `kill -CONT $pid`. Wait 5 seconds, then press **Retry**.

- **You should see:** the box shows `cleaning up the author session...` and returns to the idle text. The author session is deleted by name. On the CAO machine, `curl -s http://127.0.0.1:$CHK_PORT/sessions | grep -o 'cao-draft-[a-z]*-[0-9a-f]*'` prints nothing. No new author starts.
- **Broken means:** the session stays, or Retry starts a second author.
- **Note:** if the box says the author profile could not be read, the cached read had expired. Open a new form and repeat from step 1.

### C35 cao-server stopped with a dialog open

**Scratch server only.** If you are using the real server, write `not run: scratch server only` in the Notes column and skip this line.

1. Open the Ask-first confirmation (C13, step 6). Do not press anything in it.
2. Stop the scratch cao-server of B0 (Ctrl+C in its terminal). Wait until the Sessions header reads `CAO unreachable, retrying` (up to a minute).
3. In the confirmation press **Launch**.

- **You should see:** the confirmation closes, nothing is sent, and the form shows a red line `CAO unreachable, retrying`.
4. Start the scratch server again with the whole start block of B0 (the exports, the `mkdir`, and the guarded launch), in a terminal in the CAO repository on the CAO machine. Do not type the `uv run` line alone, because a fresh terminal has none of B0's variables:

```sh
export CAO_HOME_DIR=/tmp/cao-window-check-home CAO_API_PORT=9890 CAO_HERDR_APP_SESSION=caochk
mkdir -p /tmp/cao-window-check
if [ "$CAO_HOME_DIR" = /tmp/cao-window-check-home ] && [ "$CAO_HERDR_APP_SESSION" = caochk ]; then
  uv run cao-server --port 9890 --terminal herdr-app
else
  echo "STOP: not starting the server: these values are not the scratch ones"
fi
```

   Then check on the CAO machine that no new session was created.
- **Broken means:** a session is created, or the window hangs.

### C36 No draft author session is left

At the end of Part C, on the CAO machine:

```sh
curl -s http://127.0.0.1:$CHK_PORT/sessions | grep -o 'cao-draft-[a-z]*-[0-9a-f]*'
```

- **You should see:** no output. The Sessions list in the window has no row with a `draft` tag.
- **Broken means:** a `cao-draft-...` name. Delete it with `cao shutdown --session <name>` and report which line left it.

### C37 An allowed-tools override with `*` takes the YOLO dialog

1. Click `+` in the **Sessions** header. Choose a profile, type the working directory, and leave **Tool policy** on **Ask first**. Click **Advanced**. In **Allowed tools** type `fs_read, *`. Leave **Session name** empty. Press **Launch**.

- **You should see:** no request. The **Session name** field shows `a session name is needed to confirm YOLO` and has the keys.
2. Type `chk-star` as the session name. Press **Launch**.

- **You should see:** the red YOLO confirmation, `Launch <profile> with every tool allowed?` (or the `--yolo` title on `kiro_cli` and `opencode_cli`), with `Type the session name to confirm: chk-star`. Press **Esc** to cancel.
3. Replace the allowed tools with `fs_*,execute_bash,web_fetch`. Clear the session name. Press **Launch**.

- **You should see:** the ordinary Ask-first confirmation, `Launch <profile> on <provider>?`, with `Allowed tools (your override): fs_*, execute_bash, web_fetch` and `--allowed-tools replaces the profile's policy`. There is no typed confirmation. Press **Esc** to cancel.
- **This is a known limit, not a failure:** such a named list is as powerful as YOLO and skips the typed confirmation. A glob inside a name (`fs_*`) is a name, not `*`.
- **Broken means:** step 1 or 2 shows the Ask-first dialog or sends a launch without the typed name.

### C38 Create schedule on a server without `POST /flows/validate`

This needs a cao-server that answers 404 or 405 for `POST /flows/validate` (an older CAO). On a current server write `n/a: not testable against a new server` in the Notes column. To read the behavior without an old server, run `node lib/flows-form.test.mjs` from `examples/herdr-app-cao-window/` and read the cases whose names start with `Validate route missing`.

1. Point the window at the older server (install with `--cao-port <its port>`), open **New schedule** (`Schedules`, then `+`), and fill every field.

- **You should see:** **Create schedule** and **Create and enable...** are dimmed and do nothing when pressed. The label `server checks skipped` stands beside them. A red line reads `This server cannot check a schedule first, and its create would arm the schedule and overwrite a schedule with the same name. Create it in a terminal on the CAO machine with ` + "`cao schedule add <file>`" + `, then keep it from running with ` + "`cao schedule disable <name>`" + ` until you enable it here.` The next-runs area reads `needs a CAO change: POST /flows/validate`.
2. Close the dialog. Open the details of an existing schedule in the right sidebar.

- **You should see:** **Edit** is dimmed, with `Edit: needs a CAO change`.
- **Broken means:** a schedule is created from the window, or an existing schedule's file changes. On the old server `curl -s http://127.0.0.1:<port>/flows` must not list the schedule you filled in.

### C39 A refused author profile shows its refusal

Draft refuses a profile that does not meet the three rules. Check the refusals, then a good profile (C22).

1. Install with a profile that has no `provider` of its own and one MCP server, such as the built-in `developer`: `--prop 'draftProfile="developer"'`. Quit and reopen the window. In **New schedule** (`Schedules`, then `+`) type a description and press **Draft**.

- **You should see:** nothing starts. The box shows `kiro_cli is an install-time provider: its tool policy is the installed agent's, so a launch override cannot restrict the author. Pick another provider in draftProviders.` (A profile without a `provider` resolves to `kiro_cli`.)
2. Make a copy of `chk-draft-author` named `chk-draft-nomcp` with the `mcpServers` block removed, install it, and use `draftProfile="chk-draft-nomcp"`.

- **You should see:** `The author profile chk-draft-nomcp must declare exactly one MCP server, cao-mcp-server: with none declared the agent uses the MCP servers of whoever runs it, so the author's tools cannot be limited to CAO's own. Add cao-mcp-server to the profile's mcpServers.`
3. Add a second server to a copy named `chk-draft-two` (any name under `mcpServers` next to `cao-mcp-server`).

- **You should see:** `The author profile chk-draft-two declares an MCP server other than cao-mcp-server, so the author's tools cannot be limited to CAO's own. Use a profile whose only MCP server is cao-mcp-server.`
4. Add `native_agent: <any name>` to a copy named `chk-draft-native`.

- **You should see:** `The author profile chk-draft-native is a native agent: its MCP configuration lives in the agent's own file, which this window cannot see, so the author's tools cannot be limited to CAO's own. Use a profile that is not a native agent.`
5. Install with the good profile again and run C22 to see it work: `chk_install --prop 'draftProfile="chk-draft-author"'`, then restart the window.
- **Broken means:** a refused profile starts an author session. Check `curl -s http://127.0.0.1:$CHK_PORT/sessions | grep -o 'cao-draft-[a-z]*-[0-9a-f]*'` prints nothing after each refusal.
- **Known limit:** Agent Plugins installed on the CAO machine add their MCP servers at launch, and the window cannot see them. Write down any such plugin on your CAO host.
- Each step needs the installer run with its own `chk_install --prop 'draftProfile="<profile>"'` and a window restart. Each install derives the layout from the defaults and the `--prop` options you pass, so an install without `--prop 'draftProfile=...'` clears the profile. If that is too slow, do steps 1 and 2 and write `not run` for 3 and 4.

### C40 Schedule limits

In **New schedule**, with the other fields filled:

1. Type the schedule `0 0 31 2 *` (31 February).

- **You should see:** under **Schedule** a red line `this schedule never fires`. **Create schedule** stays dimmed.
2. On the Mac run `printf '%s %s %s * *' "$(seq -s, 0 59)" "$(seq -s, 0 23)" "$(seq -s, 1 31)" | pbcopy` and paste the result into **Schedule**. It is about 320 characters.

- **You should see:** a red line `is too long (at most 256 characters)`.
3. Type `0 9 * * 1-5`.

- **You should see:** the weekday warning from C17 (`This server counts days of the week from Monday as 0 ...`). Use `mon-fri`.
- **Broken means:** a never-firing or over-long schedule is accepted, or the check takes many seconds to answer.

### C41 Request bodies show in `ps`, env values never do

The window sends request bodies as a curl argument on the CAO machine, so they are visible in `ps` for as long as curl runs. This line checks that, and that env values are never among them.

1. On the CAO machine start: `while true; do ps -eo args | grep '[d]ata-binary'; sleep 0.2; done`.
2. In the window launch a session as in C14, with the session name `chk-ps` and the message `body-visible-check`.

- **You should see:** while the launch runs, a `curl` line with `--data-binary` and `{"initial_message":"body-visible-check"}`. Stop the loop with Ctrl+C.
3. Fill an env row as in C16 and try to launch.

- **You should see:** Launch is disabled, nothing is sent, and the loop prints no line holding the env value.
- **Broken means:** the env value appears in `ps`, or in any request.
- **Remember:** do not type a secret into a message, a schedule prompt, workflow source, workflow run inputs, or a Draft description.

### C42 Known gap note, best guess, and "stuck?"

**Known gap and "done".** Select any session and look at the **Agents** block.

- **You should see:** neither the sentence `"Done" means the agent's turn ended.` nor the known-gap sentence and its label anywhere in the Agents block. They are in the help of the status bar only (C55).
- **Broken means:** a known-gap or "done" note in the Agents block.

**Best guess.** Use the supervisor session (C0) on the scratch server. On the CAO machine delete its root, the oldest terminal, and leave the workers (the port 9890 is written out on purpose, so this never deletes a terminal on your real server):

```sh
curl -s http://127.0.0.1:9890/sessions/SESSION_NAME/terminals | jq -r '.[] | "\(.id) \(.agent_profile)"'
curl -s -X DELETE http://127.0.0.1:9890/terminals/ROOT_ID
```

Replace `SESSION_NAME` with the supervisor session's name (for example `cao-chk-sup`) and `ROOT_ID` with the id of the root, the oldest terminal, from the first command's output. The placeholders have no quote or angle-bracket characters, so a command pasted unedited fails with an error instead of leaving the shell waiting for a closing quote. The second command deletes nothing until you replace `ROOT_ID`. Run it only on the scratch server.

- **You should see:** the session stays in Sessions with the oldest remaining worker shown under it, and the label `best guess` after that worker's name. The same label shows beside its name in the **Agents** block.
- **Unconfirmed:** that the first row of that list is the root, and that deleting a root leaves its workers running. The route's own comment says index 0 is the oldest terminal. If the whole session disappears, write `not run`.

**"stuck?"** Install again with a short quiet time and restart the window: `chk_install --prop quietMinutes=1`. Give a worker a long job that needs no permission, for example ask the supervisor to assign a worker to read and summarize every file under the working directory with `fs_read`. Watch the session while the worker works.

- **You should see:** after the worker's status has not changed for one minute (counted from the later of its last change and the window's start), the session line shows `stuck?` in red and the small label `heuristic` beside it. Click `stuck?`: the session is selected and its agents show in the **Agents** block.
- **Not a failure:** if the worker's status keeps changing, `stuck?` correctly does not show. Write `not seen`.
- **Broken means:** `stuck?` shows without the `heuristic` label, or on a worker that is not working.

### C43 Workflow Cancel and Resume are confirmed

You need a running run. Approve the plan of `chk-wf-py` as in the optional step of C21 (this starts real agents on the scratch server), submit again, and work inside the first minute.

1. In the right sidebar, in the details of `chk-wf-py`, press **Cancel** on the `running` run in its **Runs** list.

- **You should see:** a confirm card titled `Cancel run <first 8 characters of the id>...?` with `This asks the run to stop. Steps already finished are kept.` and the buttons **Keep it running** (focused) and **Cancel run**. Press **Enter** without moving: the card closes and the run keeps going.
2. Press **Cancel** again, then **Cancel run**.

- **You should see:** the row note `Cancel requested. The run shows cancelled after the next refresh.`, then the state `cancelled`.
3. Press **Resume** on the cancelled run.

- **You should see:** a confirm card titled `Resume run <id>...?` with `This starts the run again from its journal. Its agents work with nobody watching, and a step that needs plan approval stops again.` and the buttons **Not now** (focused) and **Resume run**. Choose **Resume run**: the note reads `Resume requested. The run shows its new state after the next refresh.`
4. Cancel the run once more to stop the agents.
- **Broken means:** a run is cancelled or resumed without the card, or **Enter** on the first card cancels the run.

### C44 Schedule... from a workflow, and "Scheduled by"

1. Click `Workflows` and open `chk-wf-py` in the right sidebar. Under **Scheduled by** the line reads `No schedule runs this workflow.` Below it: `Workflows have no scheduler of their own: Schedule... creates a schedule that runs this one.`
2. Press **Schedule...**.

- **You should see:** the dialog with **New schedule**, **A workflow** chosen and `chk-wf-py` selected, the line `Schedule... creates a schedule that runs workflow chk-wf-py. The workflow does not change.`, a red warning `An unattended run stops at plan approval unless the plan is approved for these inputs.`, and a **Prompt the schedule sends** box that starts `Run the CAO workflow "chk-wf-py" with this command:` and holds the line `cao workflow run chk-wf-py`.
3. Name the schedule `chk-sched`, click **Weekdays 09:00**, pick a profile, and press **Create schedule**. The dialog closes and the right sidebar shows the details of `chk-sched`.
4. Click the back arrow, click `Workflows`, and open `chk-wf-py` again.

- **You should see:** under **Scheduled by** a row `chk-sched`, the schedule `weekdays at 09:00, 0 9 * * mon-fri`, and `disabled`. Click the row: the schedule opens in the right sidebar.
- **Broken means:** the schedule is created enabled, or the workflow's row lists no schedule.

### C45 A duplicate schedule name

1. Click `Schedules`, then `+`. Type the name `chk-flow` (made in C17) and fill the other fields.

- **You should see:** under **Name** the red line `a flow with this name already exists`, and **Create schedule** dimmed.
2. To see the server's own 409, use a name that is free when you check it. On the CAO machine save this as `chk-race.md`:

   ```yaml
   ---
   name: chk-race
   schedule: "0 9 * * mon-fri"
   agent_profile: developer
   provider: claude_code
   ---

   Say hello.
   ```

   In the window fill a new schedule named `chk-race` (any profile and prompt) and wait a second, until **Create schedule** is no longer dimmed. On the CAO machine run `cao schedule add chk-race.md` and then `cao schedule disable chk-race`. In the window press **Create schedule**.

- **You should see:** the form stays, with `a schedule with that name exists` under **Name**, and nothing else changes. (If the window re-checked the form first, you see `a flow with this name already exists`, which is also correct.)
- **Broken means:** the file of `chk-race` is overwritten (check with `cao schedule list`), or a second schedule appears.
- Remove it afterwards: `cao schedule remove chk-race`.

### C46 New session Advanced fields

Click `+` in the **Sessions** header, choose a profile and a working directory, type `chk-adv` as the session name, and click **Advanced**.

1. Click the provider chip `claude_code`, then `kiro_cli`.

- **You should see:** no **Kiro engine** field and no **Resume a Claude Code conversation** field for either provider, in the form or in Advanced, and no `--engine` or `--resume-session-id` in the **Equivalent command**. (The window launches with whatever engine and setup CAO has for the profile; for a Kiro engine or a resumed Claude conversation use `cao launch` in a terminal.)
2. Click **Memory manager** (`--memory: also starts a context-manager terminal`).

- **You should see:** the command gains `--memory`. Launch with it (Auto-approve, a `claude_code` provider): the session also holds a `memory_manager` terminal, which counts as a worker (`1 worker`).
3. Type `fs_read` in **Allowed tools**.

- **You should see:** the hint reads `Replaces the profile's tool policy`, the command gains `--allowed-tools fs_read`, and **Launch** with **Ask first** opens the dialog with `Allowed tools (your override): fs_read` and `--allowed-tools replaces the profile's policy`. After a launch the card lists `Allowed tools recorded by the server: ...`.
- **Broken means:** an engine or resume field shows, or a field is missing from the command.

### C47 Sessions: one line per session

- **Do:** look at the **Sessions** block with the supervisor session (C0) and one more session, and with one worker blocked (C10) if you have one.
- **You should see:** one line for each session, and no line for an agent or a worker. Each line is a small rounded badge with the first letter or digit of the session's name after its `cao-` prefix (`cao-aria-...` shows `A`, `cao-ws-...` shows `W`; the letters differ between sessions, not all `C`; the size, radius, bold letter and colours of a space in the normal Herdr window's Spaces list: compare them), then the session's name, then, when one applies, the orange `!` square of needs you (a session with a blocked terminal) or a green dot (a talkable agent has replied and the status mark below does not already show it), then at the right end the status mark of the session's first talkable agent (drawn as Herdr's own Agents list draws it: a spinning arc for working, a ring for idle, a filled dot for done and the same dot in green for replied, an orange `!` square for needs you and for error, nothing for a state Herdr does not know; compare it with the same agent in a normal Herdr window). The names are in the regular weight of your `font_weight` setting, as the rows of the normal Herdr window's Spaces and Agents lists (compare them side by side: ours must not look bolder); the block titles `Sessions` and `Agents` are the medium weight like Herdr's `Agents` heading. Two sessions never share a badge colour until all six colours are in use; a session keeps its colour after a restart. There is no state word on the line; hold the pointer on the line for half a second and the tooltip reads `<session>: done` (or idle, working, replied, needs you). A session with a blocked terminal is listed first. A recorded Draft with AI author session shows the tag `draft` and no extra mark. Click a line: its first talkable agent shows in the pane, and the line is highlighted.
- **Broken means:** a worker listed under a session line, the needs-you mark missing for a blocked session, a word such as `Asking` or `Replied` drawn on a row, or a click that selects but leaves the pane on another terminal.
- **Long lines:** make the sidebar narrow (drag its edge: it stops at 200 px; it starts at 268). Every row stays one line: a long session name ends in `…` and its tooltip has the whole name; the marks before the state mark, and `stuck?` with the label `heuristic`, are fully drawn and the name gives way to them. (C62 checks the same at every width.)
- **Report back:** a row that wraps onto a second line, or a mark or label that is cut.

### C48 The New session control

- **Do:** click the `+` in the **Sessions** header. Close what opens. Look at the end of the session list.
- **You should see:** the New session dialog opens. With it open, a second click on the `+` opens nothing and does not disturb the open dialog. The list ends with its last session: there is no `+ New session` row under it.
- **Broken means:** no dialog, a second dialog on top of the first, or a `+ New session` row at the end of the list.

### C49 Agents: the tree and its indentation

- **Do:** select the supervisor session (C0) in **Sessions**. Look at the **Agents** block, then pin one worker with its pin marker.
- **You should see:** the header `Agents · <session>`, then one entry for each agent of the session (all of them, however many): the conductor first, then the workers it started, indented one step for each ancestor that is shown, and no terminal id printed as "started by". Each entry is one line: the logo of the agent's program (the Claude Code glyph for a Claude Code agent, the Kiro glyph for a Kiro one, Herdr's generic bot glyph when Herdr has none; compare the logo of the same agent in Herdr's own Agents list), its name (the name of its Herdr tab, for example `conductor-774e`, never a bare terminal id), how long it has been in its state (`12m`, `3h`), the agent's status mark at the right end, and for a worker the pin marker after it. The status marks of all rows are in one column, with or without a pin. The session's main agent (its first talkable agent, the one the Sessions line speaks for) has a small accent-coloured crown in the slot at the right end of its row, where a worker has its pin (the status marks of all rows stay in one column, and its name has the room the crown no longer takes); a worker, and a second talkable agent when a profile is listed in `talkableProfiles`, have none, and the Sessions line's letter badge has none. A long name ends in `…` and the crown stays visible at the right. The tooltip of the line holds the state in words and the time in it, the turn, `talkable` or `worker`, and the seen state for a talkable agent. A name too long for the line ends in `…`. A worker whose caller is not in the session shows `parent not listed`.
- **Click an agent entry:** the pane shows that terminal and the selected session does not change.
- **Broken means:** a worker shown at the conductor's level while its caller is shown, a worker left out of the list, or a click on an agent that changes the selected session.

### C50 The filter header and its clear control

- **Do:** click `1 asking` in the status bar (C10 gives you one asking session), then look at the **Sessions** header.
- **You should see:** at the default 268 px width `1 of N · asking` with a small `clear` control (the header drops `filter:`, then `Sessions`, to keep the count and the filter's name readable; its tooltip reads `Sessions 1 of N · filter: asking`, and a sidebar dragged wide enough shows that whole), and only the matching sessions listed. A recorded draft author session, if any, is still listed with its tag and is not counted in `1 of N`. Click `clear`: the header reads `Sessions` and every session is listed again. Click `1 asking` twice instead: the second click also clears it.
- **Broken means:** a header that does not say a filter is on, no `clear` control, or a count that includes a draft session.

### C51 "Started by schedule X" opens the right sidebar at that schedule

- **Do:** with schedule `chk-flow` (C17) created, run it with Run now or `cao flow run chk-flow` so the session `cao-flow-chk-flow` exists, and select that session. In **Agents** click `Started by schedule chk-flow`. Do it once with the right sidebar closed and once with it open at the Workflows list.
- **You should see:** the line `Started by schedule chk-flow`, the schedule in words and the label `inferred from the session name`. The click opens the right sidebar with the details of `chk-flow` under a back arrow, within a second or two, and the selected session does not change.
- **Broken means:** the sidebar stays closed, opens at the list instead of the schedule, or the selected session changes.

### C52 The top bar with one shown agent shows its one tab

- **Do:** select a session whose only shown agent is its conductor (no pinned worker).
- **You should see:** the top bar above the pane is drawn at its full height with one tab at the left, the conductor's: the logo of its program, its name as the **Agents** block names it, and the crown, with no status mark (its tooltip says the conductor's state in words); the `Schedules` and `Workflows` buttons are at its right, each with its icon before the word, looking like the `Files` and `Changes` buttons of a normal Herdr window. Pin a worker (C49): a second tab appears and the bar's height does not change. Select a session with no talkable agent and no pinned worker: no tab, and the bar and its buttons stay. Click a tab: the pane shows that terminal.
- **Broken means:** no tab for the one agent, a status mark on the tab, the bar disappearing or changing height, or a button missing.

### C53 The Schedules and Workflows buttons

- **Do:** look at the buttons, then click `Schedules`, then `Workflows`, then `Workflows` again. With the sidebar closed, run `cao schedule disable chk-flow` (or enable it) on the CAO machine and wait up to 30 seconds.
- **You should see:** the buttons read `Schedules` and `Workflows`, with no count and no `all off`, whatever the lists hold. Each looks like the `Files` and `Changes` buttons of a normal Herdr window: a small outline icon before the word (a stopwatch for `Schedules`, a fork for `Workflows`), no filled background and muted text at rest, a hover fill, and the selected fill with full-colour text while its list is open. The first click opens the right sidebar at the schedules list, the second shows the workflows list, the third closes it. The button of the open list is drawn active. The list's title in the right sidebar reads `Schedules`, and its tooltip says `A schedule is a CAO flow: ...`. The list's own header, in the right sidebar, still carries its count (`5, all off`). A thin vertical rule (1 px, about 16 px high) separates the tabs from the buttons and the last tab never touches it. Make the window narrow, or open a session with more agents than fit: the tab strip scrolls sideways with a scrollbar like the tabs of a normal Herdr window, the focused tab stays in view, a turn of the wheel over the bar steps to the previous or next tab, and the rule and both buttons stay whole at the right and do not scroll.
- **Broken means:** a count or `all off` on a button, a missing icon, a button with a resting fill or outline that Herdr's `Files` button does not have, a button that stays active after the sidebar closed, or a button that is cut off.

### C54 The status bar parts are buttons

- **Do:** look at the status bar. Move the pointer over `replied`, then click `asking`, then `sessions`.
- **You should see:** every count (`sessions`, `asking`, `replied`, `working`) has a faint filled background at rest (no outline), a hover colour under the pointer, and a highlight while its filter is on. `asking`, `replied` and `working` filter **Sessions** and a second click clears the filter. `sessions` clears any filter. The lead word `cao` and `CAO unreachable` are text, not buttons.
- **Broken means:** a count with no faint filled background at rest, a count drawn with an outline, a highlight on the wrong part, or the `sessions` part that does nothing while a filter is on.

### C55 The "?" help and its two notes

- **Do:** click the `?` button at the end of the status bar.
- **You should see:** a small dialog with the sentence `"Done" means the agent's turn ended.`, the sentence `An agent that finished by asking you something in prose is not counted as asking.` and the label `known gap: a done agent asking in prose is not counted`. Esc closes it. Neither sentence appears in the **Agents** block or anywhere else.
- **Broken means:** no dialog, a missing sentence or label, or either sentence shown in the Agents block.

### C56 The right sidebar: a list, its details and the back arrow

- **Do:** with schedule `chk-flow` (C17) and one workflow listed, click `Schedules`, click the row `chk-flow`, then click the back arrow. Click `Workflows`, click a workflow row, then the back arrow. Select another session while `chk-flow` is open in the right sidebar.
- **You should see:** the list has the list's name, its count in words, a `+`, and one row for each schedule or workflow with its state in words (`off`, the next run, `running`, `never run`, `succeeded`). A row click shows the item's details in the right sidebar under a back arrow and the arrow returns to the list; nothing is sent by the arrow. The details of a schedule show its schedule in words and cron, the next three runs, the last run, the target and the live session. The details of a workflow show its description, mode and step count, `Scheduled by`, and its runs. Selecting another session changes neither the sidebar nor its details.
- **Broken means:** a row that opens nothing, a back arrow that sends a request or leaves the right sidebar's details, or details that change when the selected session changes.
- **Report back:** anything cut off at the narrow width.

### C57 "Show prompt" keeps the prompt collapsed

- **Do:** open `chk-flow` in the right sidebar, look at the Target section of its details, click `Show prompt`, then `Hide prompt`. Open a schedule that has no prompt in the right sidebar, if you have one.
- **You should see:** the prompt text is not shown until you click `Show prompt`; the control then reads `Hide prompt` and the text is shown whole; `Hide prompt` collapses it. A schedule with no prompt reads `not available` and has no control. Going back to the list and opening the schedule again shows it collapsed.
- **Broken means:** the prompt shown on first open, or a control that does nothing.

### C58 The dialog: size, first read and a closing key

- **Do:** click the `+` in the **Sessions** header. Look at the dialog, then look at the profile list. Type a word in the message. Press Esc. Press Esc again, and with the dialog open again, start a slow launch and press Esc.
- **You should see:** a centered dialog that covers most of the window, with a title, a body that scrolls and a row of buttons at the bottom. The profile list fills in and the dialog never says `CAO unreachable` while the server answers (the dialog got the layout's CAO machine and port). The first Esc shows `Discard this form?` in place of the form body with `Keep editing` as the default; Esc again returns to the form with your text. The dialog reads nothing from what is selected behind it (a modal dialog dims the window, so a selection cannot be changed by hand; the rule is tested in node and by a source scan). During a launch (use a slow one), Esc and Close say `A request is in progress. Wait for the answer.` and the dialog stays.
- **Broken means:** a dialog that is small or uncentered, a profile list that stays empty, `CAO unreachable` while the server answers, or an Esc that closes the dialog and loses the text.
- **Report back:** the size of the dialog in the window you used.

### C59 Esc with edits, in each form and in a confirmation

- **Do:** for New session, New schedule and New workflow in turn: open the form, type one character in a field, press Esc. Then open New session, choose Ask first, press Launch to reach the confirmation, and press Esc there. Open New schedule and press Esc without typing. Then open New session, click the Agent profile field so that its list shows, press Esc (the list closes), and press Esc again.
- **You should see:** Esc with a typed character asks `Discard this form?` with `Keep editing` as the default, in each form. Esc inside the Ask-first confirmation returns to the form body and the dialog stays open. Esc on an untouched New schedule closes the dialog at once, with no question. In the Agent profile field, the first Esc closes its list and the second Esc, with the list closed and the keys still in that field, asks `Discard this form?` when something was typed (and closes at once when not).
- **Broken means:** a form that closes on the first Esc with text typed, an Agent profile field from which Esc never closes the dialog, a confirmation that Esc closes together with the dialog, or an untouched form that asks.

### C60 Create and enable, confirmed inside the dialog

- **Do:** click `Schedules`, then `+`. Fill `chk-flow-2` with a valid schedule, a profile and a prompt, and wait for `Next 3 runs` to fill in. Press `Create and enable...`. Press Esc. Press `Create and enable...` again, press Tab, then Enter.
- **You should see:** `Create and enable...` replaces the form body in the dialog with `Create schedule chk-flow-2 and enable it?` and the warning that the schedule launches an agent with nobody watching, the focus on Cancel; the form's buttons stay at the bottom, dimmed. Esc returns to the form with every field as it was and sends nothing. Tab then Enter sends `POST /flows` with `enabled: true`; the dialog closes and the right sidebar opens (if it was closed) on the details of `chk-flow-2`, which read `Enabled`. A plain `Create schedule` instead creates it disabled.
- **Broken means:** a confirmation drawn in the sidebar or over the dialog instead of inside it, an Esc that closes the whole dialog, a request sent before Confirm, or a sidebar that does not show the new schedule.

### C61 A draft closed mid-draft leaves no author session

Needs the author profile of C22 (`chk-draft-author`), installed in the window.

- **Do:** three steps, each from `Schedules` then `+`. (1) Type a description, press `Draft`, and while the box says `drafting...` press Esc, then `Discard`. (2) Do it again, and while the box says `drafting...` switch the layout so that Herdr closes the dialog without asking (or quit with Cmd+Q). (3) Do it a third time and close the dialog with Esc and `Discard` within a second of pressing `Draft`, before the box shows a session name. After each step, wait 30 seconds, then on the CAO machine run `curl -s http://127.0.0.1:$CHK_PORT/sessions | grep -o 'cao-draft-[a-z]*-[0-9a-f]*'`.
- **You should see:** the first Esc asks `Discard this form?` while the author is out. After each step and at most 30 seconds, the `curl` prints nothing, and Sessions shows no row with a `draft` tag. Nothing is created in Schedules.
- **Broken means:** a `cao-draft-...` name 30 seconds after the dialog went. Delete it with `cao shutdown --session <name>` and report which step left it.
- **Report back:** for each step, how many seconds passed before the name was gone from the `curl`.

### C62 No label is clipped at the narrowest widths

- **Do:** make the window as narrow as the layout allows, then check four places. (1) The right sidebar at its smallest width (280 px): open `Schedules`, open a schedule with a long name, and read its Runs note and the rows of the list. (2) The Sessions block at its narrowest (200 px): find a session whose line holds the needs-you mark and, for a worker that is quiet, the hint `stuck?` with its label `heuristic`; if there is none, use a session with a long name. (3) The Agents block at 200 px: select a session started by a schedule (`Started by schedule <name>` with the label `inferred from the session name`) and one with a recap (`the agent's own words`). (4) Make the window as small as Herdr allows (480 by 320) and open New session (the `+` in the Sessions header), then `Schedules` and `+` with a Draft description filled so that a field shows the `AI` mark, then a confirmation with a long button label (`Create and enable...`).
- **You should see:** every honesty label and every mark whole. In the right sidebar and the dialog a label that does not fit on the line of its value continues on the next line of the same row, and a long name wraps instead of ending in `...`. In the left sidebar every row is one line instead: a name or a path ends in `…` and its tooltip has the whole text, while the marks and the label beside it are fully drawn. The one exception is the recap: its label `the agent's own words` is on a small line of its own, never cut, and the recap's text under it wraps over up to three lines at the size of the agent rows and ends in `…` after the third when it is longer, with the whole text in its tooltip. The `AI` mark, the flag after a field's name, a chip with a long profile or workflow name and the buttons of a confirmation are fully drawn, and the dialog's body scrolls instead of cutting a row.
- **Broken means:** an honesty label or a mark ending in `...` or cut at the edge, a row of the right sidebar or the dialog with a fixed height that hides its second line, a row of the left sidebar that wraps onto a second line (the recap is the one text that may take three), a recap longer than three lines, a recap cut to one line, or a button label that runs past its box. Write down which of the four places and at what width.
- **Report back:** the narrowest width at which each of the four places was still whole.

### C63 The dialog's open state: a lease

- **Do:** three steps, in this order. (1) Open New session with the `+` in the Sessions header, press Esc on the untouched form, and click the `+` again at once. (2) Open the dialog, then switch the layout so that Herdr closes it without asking (or quit with Cmd+Q and open the window again with the dialog open), and click the `+` in the Sessions header after 12 seconds. (3) Leave a dialog open for 15 seconds without touching it, then click `+ New session`.
- **You should see:** (1) the dialog opens on that next click, with no wait. (2) The dialog opens, at most 10 seconds after Herdr closed the other one. (3) The click is refused and no second dialog opens.
- **Broken means:** a first click that is refused after a plain Esc, a dialog that stays refused for more than 10 seconds after Herdr closed it, or a second dialog on top of the first.
- **Report back:** how many seconds the refusal lasted in step 2.
- **Alternative:** the status bar `?` dialog (C55) can stand in for the form in steps 1 to 3.

### C64 Restart with the right sidebar left open

- **Do:** open the right sidebar with `Schedules`. Quit the window with Cmd+Q and open it with the launcher.
- **You should see:** after about one second the sidebar is hidden, the pane uses its width, and neither `Schedules` nor `Workflows` is drawn active. Click `Schedules` once: it opens on the first click.
- **Broken means:** the sidebar open after a restart, a button drawn active while the sidebar is hidden, or a first click that does nothing.

### C65 An answer after leaving the right sidebar's details

- **Do:** with two schedules `chk-flow` and `chk-flow-2`, open `chk-flow` in the right sidebar, press Run now and confirm. At once click the back arrow and open `chk-flow-2`. Look at the list while the run is out, then open `chk-flow` again after the answer.
- **You should see:** while the request is out the row of `chk-flow` reads `running`, and the right sidebar's details of `chk-flow-2` show nothing about it. When the details of `chk-flow` are shown in the right sidebar again they show the answer (`The schedule ran.` or `The run was skipped ...`). Nothing of that answer is shown on `chk-flow-2`.
- **Broken means:** an answer shown on the wrong schedule, an answer lost because the right sidebar's details were left, or a row that does not read `running` while the request is out.

### C66 A workflow Run in the dialog: plan approval, the "not submitted" row, and nothing typed is stored

Use a workflow that was never approved (`chk-wf-py` from C31). Do not approve it in a terminal unless you want a real agent run. Make a distinctive value for the leak check in the shell that runs this checklist: `MARK="chk-$(date +%s)-marker"; echo "$MARK"`. Use it, and the directory `/tmp/$MARK-dir`, where the steps below say `$MARK`.

- **Do:** click `Workflows`, open `chk-wf-py`, press `Run`. If the form has an input, type the value of `$MARK` in the first one (or in the name and value rows); type `/tmp/$MARK-dir`, with `$MARK` replaced by its value, in `Working directory`. Press `Submit`. Then press Esc, open `chk-wf-py` in the right sidebar again, and press `Run` once more. Close that second dialog with Esc. In the shell that runs this checklist (where `$HERDR_APP_HOME` is not the window's home), run `ls "$CHK_HOME" | head`, which must list `shared.json` and `mods`; then `grep -rl -e "$MARK" "$CHK_HOME"` and `grep -rl "chk-wf-py" "$CHK_HOME"`. If `shared.json` is not written until the window quits, quit it and run both greps.
- **You should see:** after `Submit` the dialog stays open and shows `Plan approval needed. Approve it in a terminal`, `Plan id: <the id>`, the command `cao workflow approve <the id>` with `Copy`, and the button `I approved it: submit again`; there is no button that approves the plan. Esc closes it with no question. In the right sidebar's details, the Runs list holds a row `not submitted` marked `approval needed`, and the second `Run` opens an empty Run form. The first `grep` lists no file. The second lists `shared.json`.
- **Broken means:** the marker value or the directory in any file of `$CHK_HOME` (report the file name, and do not paste the value anywhere else), an approve button, a dialog that closes on the 403, or a `not submitted` row that is gone after the dialog closed. A second `grep` that lists no file is a blind check and does not count: the row holds the workflow's name, so the window's own record must be visible to it.
- **Optional:** run the printed command in a terminal on the CAO machine, reopen `Run`, type the values again and press `Submit`. A 202 closes the dialog and the run shows in the right sidebar's details of `chk-wf-py`; the `not submitted` row is gone. This starts real agents: cancel the run afterwards.

### C67 The Agents block highlights the terminal you are in

- **Do:** select the supervisor session (C0) in **Sessions**. Click one agent's row. Click a tab in the top bar (pin a worker first, C08, so there are two). Then move the focus without touching the Agents block: press the key bound to `agent.next` (or run `herdr-app do agent.next`), and switch to another session's space with `herdr-app do space.select --id <space id>` and back.
- **You should see:** the row of the agent whose terminal has the focus is drawn on the same fill as the selected session's row, after each of the four moves, and only that row. In a session with many agents, the focused worker's own row has the fill (scroll the block to see it). While the focus is on a terminal of another session (or on a non-CAO terminal) no row of the Agents block has the fill, and the selected session does not change.
- **Broken means:** a highlight that stays on the row you clicked after the focus moved, a highlight that needs a click on the block to appear, two rows highlighted, or the Agents block switching to another session.
- **Report back:** which of the four moves did not move the highlight.

### C68 Mission Control and Cmd-` list the window as CAO

- **Do:** with the CAO window open (C01) and your main Herdr window open too, open Mission Control, then press Cmd-` (the key that switches between the windows of one app) and read the window names. Quit the CAO window and start it again with the launcher, then read the name once more.
- **You should see:** the CAO window is named `CAO` and your main window is named `Herdr`, in Mission Control and in the Cmd-` switcher, and again after the restart. The Dock and Cmd+Tab name come from the app copy (C71), and the wordmark in the left sidebar is C69's.
- **Broken means:** the CAO window listed as `Herdr`, your main window listed as `CAO`, or the name `CAO` missing after the restart.
- **Report back:** the two names you saw, before and after the restart.

### C69 The top-left of the sidebar says CAO

- **Do:** with the CAO window open (C01) and your main Herdr window open too, look at the top-left of each window. In the CAO window press the search button (the magnifier), close the picker, then press the fold button (the sidebar icon) and press it again. Quit the CAO window and start it again with the launcher.
- **You should see:** the CAO window shows the CAO mark (a hexagon around three linked dots, in blue, teal and navy) and the text `CAO` where your main window shows Herdr's logo and `Herdr`, then the search button and the fold button, on the same row height and spacing as your main window's, with room left of the mark for the traffic lights. The mark is 20 px with rounded corners, in full colour, not one tint. On a light theme the navy parts are dark; on a dark theme they are a pale tint, so no part of the mark vanishes into the sidebar (switch the theme in Settings and look again). The search button opens the picker. The fold button asks for the 76 px rail, which the sidebar's 200 px minimum clamps, so the sidebar stays at 200 px (write down what it did on yours). The sidebar's footer at the bottom (Settings, Add machine) is Herdr's own and unchanged. The text is `CAO` again after the restart, and the Agents block's header shows no folder path.
- **Broken means:** `Herdr` or Herdr's logo at the top-left of the CAO window, a missing mark or button, a mark in one flat colour, a dark mark on a dark sidebar, a search button that opens nothing, the text drawn under the traffic lights, or the footer changed.
- **Report back:** the text you read, which drawing (light or dark) you saw on each theme you tried, whether the search button opened the picker, and what the fold button did to the sidebar's width.

### C70 An agent that waits on its workers shows as waiting

Works on any CAO: the window derives the wait from who started whom (a conductor that is idle or done while a worker below it works), so an older server with no `cao.waiting` label shows it too, and the `labels` check below is only for a CAO that writes the label. Needs a conductor that really delegates with `assign` (a worker that takes a minute or more makes the waiting easy to see; C0 starts one).

- **Do:** start a session with a conductor (C0). Ask the conductor to `assign` one worker a task that takes a minute or more, and to end its turn while it waits. While the worker works, look at the session's line in **Sessions**, the conductor's row in **Agents**, its tab in the top bar and the status bar, and rest the pointer on the line and on the row. In a session with a supervisor between (conductor, supervisor, workers) the conductor and the supervisor both show it while any worker below them works (the conductor counts the supervisor too: `waiting on 2 workers`). Then, from a terminal on the CAO machine, run `HC_SESSION=<core> hc get <conductor address> --json` and read `labels` (`hc ls` has no labels). When the worker replies, watch the conductor start its next turn and finish it.
- **You should see:** while the worker works, the conductor's mark is Herdr's spinner in the muted colour (not the done dot, not the idle ring, not the bright working spinner), the line and the row show no green dot, the tooltip reads `<name>: waiting on 1 worker`, the status bar counts the session under `working` and shows no replied part, and, on a CAO that writes the label, `labels` holds `cao.waiting` with the value `1`. After the reply the label is gone, the conductor shows the working spinner while it handles the reply, and then the green done dot. With two workers assigned, the tooltip reads `waiting on 2 workers` and falls to `waiting on 1 worker` when the first replies.
- **Broken means:** the done dot or the idle ring while the worker still works, a green dot before the reply arrived, a replied count in the status bar while the conductor waits, a muted spinner that stays after the worker replied or ended (wait one 30 s sweep before calling it stuck), or a muted spinner on an agent that waits on nothing.
- **Report back:** what the mark and the tooltip looked like, the value of `cao.waiting` before and after the reply, and how long the label took to go after the reply.

### C71 The app switcher and the Dock say CAO, with the CAO icon

Needs macOS and an install made without `--no-app-copy` (the default), so that `~/Applications/CAO.app` exists. Keep your main Herdr window open as well.

- **Do:** run the installer (`sh install.sh ...`) and open the window with the launcher. Press **Cmd+Tab**. Look at the Dock. Open **Mission Control** (F3 or three fingers up). Right-click the CAO tile in the Dock and choose **Options > Keep in Dock**, quit the window with **Cmd+Q**, then run the launcher again. Then run `sh install.sh ...` a second time with the same options.
- **You should see:** the entry for the CAO window in the Cmd+Tab list and under its Dock tile reads `CAO`, with the CAO icon (a cream rounded square, the hexagon with three nodes, in Herdr's colours); your main window's entry reads `Herdr` with Herdr's icon. Mission Control names the windows `CAO` and `Herdr`. The pinned CAO tile stays in the Dock after Cmd+Q, and the window the launcher opens lights it up; the Herdr tile is not changed. The second install run prints `install: up to date`.
- **Broken means:** `Herdr` under the CAO tile or in Cmd+Tab, the Herdr icon on the CAO tile, a second CAO tile beside the pinned one, a pinned tile that opens your main window, an installer that rebuilds on the second run, or `Herdr.app` changed (its icon or name).
- **Report back:** what Cmd+Tab and the Dock show for each window, whether the icon is the CAO one at Dock size and in Cmd+Tab, and what the second run printed. If a pinned-tile click (not the launcher) opens something, say what: it starts the app without the window's home.

## Part C2. UNVERIFIED Herdr behavior

These checks cover Herdr behavior that nothing could run before this checklist: the Herdr app's API shapes the window depends on. Most pass while you do the C lines. For each one, write what you saw, even for a pass. The text under **Report back** is what to send.

### U01 Esc, Enter and Tab arrive in the dialog

- **Do:** in C13 press **Esc**, **Enter**, and **Tab** with the Ask-first confirmation open. In New session with some text typed, press **Esc** while the cursor is in a text field.
- **You should see:** **Esc** closes the confirmation and returns to the form. **Enter** acts on the focused button (**Cancel** at first). **Tab** moves the focus between the two buttons. **Esc** in a form with typed text shows `Discard this form?`. In the YOLO confirmation the typed field has the keys, and **Enter** in it confirms once the typed name matches.
- **Broken means:** a key does nothing while a confirmation is open.
- **Report back:** each key that did nothing, and where the cursor was. (U21 to U23 check the same keys against the dialog's own overlay.)

### U02 Textarea change events and `set_value`

- **Do:** type in the **Message** box of New session. Pick a template in New workflow. Discard a form that has text in a box.
- **You should see:** the **Equivalent command** follows what you type in **Message** within a second. A template fills the **Source** box. After you discard a form and open a new one, the boxes are empty.
- **Report back:** a box that shows old text, or a command that does not follow typing.

### U03 `randomUUID`, clipboard, and timers

- **Do:** launch two sessions (C13, C14). Press **Copy** in New session (C16). Click away from the profile field after typing in it.
- **You should see:** both launches work and make two different sessions (the window cannot launch without a random key). **Copy** puts text on the clipboard, and its note (`Copied, values masked`) disappears after about 2 seconds. The profile list closes about a fifth of a second after you leave the field.
- **Report back:** a launch error that mentions a key, an empty clipboard, or a note that stays.

### U04 A click on the profile list inside the 200 ms blur window

- **Do:** in New session click the profile field, type two letters, then click a name in the list. Repeat 5 times.
- **You should see:** the first click chooses the name every time (`Selected: <name>`).
- **Report back:** how many of the 5 needed a second click.

### U05 Layout resize and fold persistence

- **Do:** fold and unfold each of the two left blocks (C05). Pin and unpin a worker (C09). Restart the window.
- **You should see:** each block folds to its 34 px header row. **Sessions** and **Agents** share the column again when both are unfolded, and the free space of a folded one goes to the other. The top bar keeps its 34 px with and without tabs. Folds are the same after a restart.
- **Report back:** a block that stays small or a fold that does not persist, with the block name, or a top bar that changed height.

### U06 Arrow and Enter key names

- **Do:** C06.
- **You should see:** every key works.
- **Report back:** the name of each key that did nothing. The window reads `up`, `down`, `left`, `right`, `enter`, `p`, `tab`, `escape`, with or without an `arrow` prefix. Report if Herdr names a key differently.

### U07 The focused terminal's address form

- **Do:** click different agents in Sessions, and in the pane.
- **You should see:** the highlighted tab (C09) and the green dot clearing (C11) follow the agent you focus.
- **Report back:** a case where the highlight or the clearing does not follow focus.

### U08 Toast overlay geometry and timeout unit

- **Do:** C12, part 2.
- **You should see:** the toast is fully visible and disappears after about 6 seconds when Herdr gives it no timeout.
- **Report back:** the time until it disappears, in seconds, and whether any part of it is cut off.

### U09 `hc read` and `hc label` through the window's core

- **Do:** for the recap, use an agent whose screen has a `recap:` line (a Claude Code agent prints one). After its turn ends, select its session. For the label, do C11 and then, on the Mac:

  ```sh
  HC_SESSION="$CHK_CORE" chk_hc ls
  HC_SESSION="$CHK_CORE" chk_hc get cao/<terminal> --json
  ```

  Take `<terminal>` (for example `t7`) from the first command's output.
- **You should see:** the **Agents** block shows the recap readably: the label `the agent's own words` on a line of its own, and the recap's text under it over up to three lines (not one cut line; `…` after the third when it is longer), the whole text in its tooltip. Drag the sidebar's edge wider and narrower: the lines re-wrap to the new width. The second command's output has a label `herdr.seen` whose value equals the agent's `turn`.
- **Report back:** whether the recap appeared, and the `herdr.seen` and `turn` values. Without a recap line the Agents block shows nothing for it, which is also correct.

### U10 The turn number increments on input

- **Do:** select a session and read `turn <N>` on the conductor's line in the **Agents** block. Send it a message, wait until it finishes, and read the number again.
- **You should see:** the number is one higher.
- **Report back:** both numbers.

### U11 The terminal list on a disconnect or while loading

- **Do (remote run only):** pin a worker. Turn the Mac's Wi-Fi off for 20 seconds, then on. Also start the window fresh and watch the first seconds.
- **You should see:** while disconnected the Sessions list stays, dimmed, with `CAO machine not connected`. No pin is lost. After reconnecting the list returns undimmed. A fresh start may show an empty list for a moment and then fills.
- **Report back:** a lost pin, a blank list that stays, or what the first seconds of a fresh start show. Write `not run` for the local run.

### U12 The Dock badge

- **Do:** with one agent blocked (C10), look at the Dock.
- **You should see:** a badge `1` on the CAO window's tile, none on your main window's tile. It goes away when the agent stops being blocked.
- **Report back:** which tile has the badge and its number.

### U13 The toast icon

- **Do:** C12 part 2, with a Herdr toast that has an icon.
- **You should see:** a small icon (16 px) left of the title, or none.
- **Report back:** what the icon is, or that there is none.

### U14 Text fields created after the window starts

- **Do:** in New session, Advanced, click **Add variable**. In a workflow's **Run** form, click **Add input** (when it shows free rows).
- **You should see:** every new field accepts typing.
- **Report back:** a field that is dead. The window makes these fields when you click, not when it starts.

### U15 A 16-row Textarea holding about 120 KiB

- **Do:** on the Mac copy about 100 KiB of text: `yes 'x: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' | head -c 100000 | pbcopy`. In New workflow paste it into **Source**. Then paste it twice to pass 120 KiB.
- **You should see:** the editor stays usable. The size line reads `Size: <N> KiB of 120 KiB`. Past the limit it reads `This source is <N> bytes once encoded; this window sends at most 122880 bytes (120 KiB). Paste a smaller spec or use the CLI (cao workflow create and update take a Python file only).`, and **Validate** and **Save workflow** are disabled.
- **Report back:** how many seconds the paste took, and whether typing lags.

### U16 The 8-hex draft suffix

- **Do:** C22.
- **You should see:** the author session is named `cao-draft-flow-` plus 8 characters from `0-9a-f` (for a workflow, `cao-draft-workflow-`).
- **Report back:** one session name.

### U17 `GET /terminals/{id}/output?mode=last` for `claude_code`

- **Do:** C22 with a `claude_code` author. While the box says `drafting...`, on the CAO machine get its terminal id and read what the window will read:

  ```sh
  curl -s http://127.0.0.1:$CHK_PORT/sessions/cao-draft-flow-<the 8 characters>/terminals
  curl -s "http://127.0.0.1:$CHK_PORT/terminals/<terminal id>/output?mode=last"
  ```
- **You should see:** after the author finishes, the output holds the author's last answer with one fenced block. The form fills.
- **Report back:** if the box says `The author's output had no definition.`, paste what the second command printed once the author had answered. A second author session is needed to try again.

### U18 `POST /sessions` against the 20 s limit

- **Do:** time every launch in C13 to C15 with a stopwatch, from pressing **Launch** to the card.
- **You should see:** under 20 seconds. Over 20 seconds the form reads `no answer from CAO. The session may have started: Retry sends the same request with the same key, so it cannot start a second one.` with a **Retry** button. Press **Retry**: you get the one session, not a second.
- **Report back:** the time per provider, and for a slow one whether **Retry** gave one session.

### U19 Two windows write one CAO structure document, and the updater

Two experiments did not run: two windows writing the shared CAO structure document and the updater with two processes. This run holds exactly that two-writer state while your main window still lists the CAO machine. The scratch window of B2 looks at the scratch server, which your main window cannot see, so there is no second writer there. For this line, point the scratch window at your real CAO core for a short time. Run it with no agent working, and write `not run: no second writer` if you will not do it.

1. **Point the window at the real CAO.** From here until step 6, do not click or focus any agent in the CAO window: it writes `herdr.seen` on a real terminal when you focus a finished agent. In the Mac terminal (outside Herdr, with the variables of "Scratch names" set), remove the machine entry from the scratch window's own core, and install again against the real port and core:

   ```sh
   HC_SESSION=caocheck chk_hc machine rm cao
   export CHK_PORT=9889 CHK_CAO_CORE=cao
   chk_install
   ```

   The first command acts on the scratch core `caocheck` only (the name is written out on purpose). Your main core is not touched. The installer adds the machine again, this time with `--session cao`, and the summary line ends with `Open the window with: ...`. Quit the CAO window with Cmd+Q and open it with the launcher. Its Sessions list now shows your real CAO sessions. If it is empty, check `HC_SESSION=caocheck chk_hc machine ls` and the `CAO machine not connected` note.
2. **Note the start state.** Write down the CAO spaces and tabs your main window shows, in order, and the count from `HC_SESSION=cao chk_hc ls` on the CAO host (do not run this on the Mac in the remote run).
3. **Make the two writers work.** With both windows open, in your main window rename one CAO tab (tab A), and move another CAO tab (tab B) to a new position. Write down where B was.
4. **Watch.** Look at the CAO window, then your main window, for a minute. Do not click or focus any agent in the CAO window from step 1 until step 6: from step 1 the window points at your real CAO, and focusing a finished agent makes the window write `herdr.seen` on that real terminal (README, known limits).
5. **Put everything back.** Rename tab A to its old name. Move tab B back to the position you wrote down. Count again with `HC_SESSION=cao chk_hc ls` on the CAO host.
6. **Point the window back at the scratch server.** Remove the real machine entry from the scratch window's core, set the scratch values, and install again:

   ```sh
   HC_SESSION=caocheck chk_hc machine rm cao
   export CHK_PORT=9890 CHK_CAO_CORE=caochk
   chk_install
   ```

   Quit the CAO window and open it with the launcher. Do not go on to Part D with the real values set: D4 would refuse them, and uninstall expects the scratch ones.

- **You should see:** the same tabs in the same order in both windows after step 3, no tab that vanished or appeared twice, and after step 5 the same tabs and order as in step 2 and the same terminal count.
- **A label may appear even if you click nothing:** Herdr may focus a terminal itself when the window opens, and that can make the window write a `herdr.seen` label on one real terminal. It is a label only. Afterwards, on the CAO host, run `HC_SESSION=cao chk_hc get TERMINAL --json` (replace `TERMINAL` with the terminal's id from `chk_hc ls`) for the terminal that had focus when the window opened, and look for a `herdr.seen` label you did not expect. (`chk_hc ls --json` may not print labels, so use `get`.) Note what you find in the Notes column.
- **Updater:** do not force an update. If Herdr offers one while both windows are open, write down what happens to the CAO window when you quit one of them. After any update, reopen the CAO window with the launcher.
- **Report back:** any lost or duplicated tab, a changed terminal count, and anything the main window did to the CAO window or the reverse.

### U20 The overlay's size at the smallest window

- **Do:** make the window as small as the app allows (about 480 by 320 px). Click the `?` of the status bar, then the `+` in the Sessions header.
- **You should see:** the help overlay is 360 by 220 px or the window's size when that is smaller, with its text and `Close` reachable. The form dialog covers most of the window and scrolls; its buttons stay on screen.
- **Report back:** the measured sizes, and any button or text that cannot be reached.

### U21 Esc on a `dialog` overlay (H1)

**Status: not run.** This is the key test: it needs a person at a Mac with the window active, and nobody has run it. Until it is run, the window's Esc behavior in the dialog is a design, not a result.

- **Do:** open New session with the `+` in the Sessions header. Type one letter in the first input and press Esc once. Repeat with the caret in the message textarea.
- **You should see:** the dialog stays and shows `Discard this form?`, and a second Esc closes it (or returns to the form: the second Esc is the Keep editing default).
- **Report back:** `PASS` if the dialog stayed and asked, and `FAIL` if Herdr closed it on the first Esc (then the `window` kind is tried: U22).

### U22 Esc on a `window` overlay (H2), only if U21 failed

**Status: not run.** It is needed only when U21 fails, and U21 has not been run.

- **Do:** set `DIALOG_KIND` to `"window"` in `lib/shell.js` of a scratch copy, install it into a scratch home, and repeat U21.
- **You should see:** the same as U21. A `window` overlay may not take the keyboard by itself, and the dialog does not focus a field when it opens (that part of the fallback is not built), so click a field first; report whether the first Esc then reached the dialog.
- **Report back:** `PASS` or `FAIL`.

### U23 Tab order across the fields (H3)

**Status: not run.** The same key test as U21: a person at a Mac, window active. Herdr ignores `tab_index` on an input, so the window sets none, and the Tab order is whatever Herdr draws.

- **Do:** open New schedule (three inputs and a textarea, two buttons). Press Tab five times, then Shift+Tab five times.
- **You should see:** the focus follows the drawn order, leaves the textarea (no tab character is typed into it) and reaches the buttons.
- **Report back:** the order you saw. If it is wrong, the next step is a wrapper element around each field; it is not built yet.

### U24 Show and hide of the right sidebar, the pane's reflow, and a restart

- **Do:** on the real layout, click `Schedules` three times (open, switch to `Workflows`, close). Watch the pane while it opens and closes. Open it, quit with Cmd+Q, open the window again.
- **You should see:** the sidebar block appears and disappears, the pane gives up and takes back its width, and after the restart the sidebar is hidden whatever it was when you quit. Nothing is resized to hide it.
- **Report back:** a sidebar that does not hide, a pane that does not take its width back, a flicker of the sidebar open at start, or a button that needed two clicks. A measurement on Herdr 0.1.17 covered show, hide, the reflow and the saved state; this is the same on the installed layout.

### U25 `shared()` across the new blocks

- **Do:** with two sessions listed, click the first in **Sessions**, then the second, then pin a worker in **Agents** and watch the top bar.
- **You should see:** the **Agents** header and tree follow the click within about a second, the second block's fold state is its own, and a pinned worker shows as a tab in the top bar within about a second.
- **Report back:** a delay of more than a second, an Agents block that shows the previous session, or a fold that moved both blocks. A measurement found 10 to 30 ms for two blocks of one mod; the new block ids were not measured on a Mac.

### U26 A press on a bar control does not drag or maximize the window

- **Do:** double-click `Schedules` and double-click an agent tab (with two tabs showing). Drag the window by an empty part of the top bar. Click `Schedules` once.
- **You should see:** the double-clicks neither move nor maximize the window, a drag on the empty part of the bar still moves it, and a single click on a button or a tab still acts (the sidebar opens, the tab focuses its terminal).
- **Report back:** a window that moved or maximized, or a click that no longer reached the control. The controls stop the press so the bar's drag strip does not start a drag on them; whether the click still arrives was not verified.

### U27 A hidden sidebar copy receives the answer of a request started before it was hidden

- **Do:** open `chk-flow` in the right sidebar and confirm Run now. At once click `Schedules` to close the sidebar. Wait until the schedule has finished (30 seconds or the time the schedule needs), then click `Schedules`, and open `chk-flow` again.
- **You should see:** the right sidebar's details show the answer of the run, and the schedule's row in the list no longer reads `running`.
- **Report back:** a row that still reads `running` after the schedule finished, or details with no answer. A measurement showed that a hidden copy keeps its timers; that it receives the answer of an `exec` was not measured.

### U28 The theme key and folder of a copied settings file

- **Do:** with no window open, read the main home's settings and its themes folder: `jq -r '.theme' "${XDG_CONFIG_HOME:-$HOME/.config}/herdr-app/settings.json"` and `ls "${XDG_CONFIG_HOME:-$HOME/.config}/herdr-app/themes"`. Install into a scratch home as in Part B, then run `ls <scratch home>/themes`, open the window with the launcher and run `HERDR_APP_HOME=<scratch home> herdr-app status`.
- **You should see:** the key that names the theme is `theme`, a theme file lives at `themes/<name>.json` in the main home, the install copied exactly that file to `<scratch home>/themes/<name>.json` (and printed a note instead when the main home has no such file, as for a theme Herdr ships), and `status` lists no theme error.
- **Report back:** the key and the folder as you read them, and any theme error in `status`. The installer reads the key `theme` and the folder `themes/<name>.json` from its own reading and from Herdr's error text quoted in commit 077923fb; nobody has confirmed them on a Mac yet, and this line is that read.

### U29 The left sidebar's edge drags between 200 and 640 px

- **Do:** the window opens with the left sidebar at 268 px. Put the pointer on the edge between the left sidebar and the middle pane and drag it right to the far end, then left to the far end. Then press the sidebar's fold button (the icon at the right of the `Herdr` title in the top left of the sidebar) and press it again.
- **You should see:** the sidebar follows the pointer, stops at about 640 px on the right and about 200 px on the left, and the rows keep their one line while it moves (names end in `…` when it is narrow and show whole when it is wide). The width is the same after a restart. The layout derives the range (`min` 200, `max` 640) and sets the root row's `resizable` to true; the sidebar column keeps `resizable: false`, which locks only the boundaries between the brand, the lists and the footer. The fold button asks Herdr for 76 px (44 off macOS), which the 200 px minimum clamps, so on the scratch window it left the sidebar at 200 px: write down what it did on yours.
- **Broken means:** an edge that does not move under the pointer, a range other than 200 to 640 px, a width that is not kept over a restart, or the boundary between the Sessions and Agents blocks (not the sidebar's edge) moving the sidebar.
- **Report back:** whether the edge dragged, the narrowest and widest width you reached, and what the fold button did. The range was checked with `herdr-app do layout.resize` on a scratch window (700 px stopped at 640, 100 px and 76 px at 200, 268 px stayed 268); nobody has dragged the edge with a pointer yet.

## Part D. Uninstall and cleanup

Run the lines in this order: D6, D1, D2, D3, D5, D4. D6 only reads the manifest, D1 does the real uninstall, and D5 needs a second install that it uninstalls again.

### D1 `uninstall.sh` runs

Close the CAO window with Cmd+Q first. In a terminal that was not started from inside the CAO window, from `examples/herdr-app-cao-window/`:

```sh
sh uninstall.sh --home "$CHK_HOME"; echo "exit=$?"
```

- **You should see:** `uninstall: done. The window's home is kept as <your home>.removed-<number>; CAO and its terminals were not touched.` and `exit=0`. The launcher `~/.local/bin/herdr-cao-check` is gone (it carries the installer's header line, so uninstall removes it). `ls ~/.config` shows `herdr-app-cao-check.removed-<number>` and no `herdr-app-cao-check`. The script removed the machine `cao` from core `caocheck` and stopped that core.
- **Broken means:** a non-zero exit (a failing `hc` step keeps the home so you can run it again), or a launcher left behind.

### D2 Your main home is untouched

```sh
ls ~/.config/herdr-app | sort > /tmp/main-home-after.txt; diff /tmp/main-home-before.txt /tmp/main-home-after.txt
find ~/.config/herdr-app \( -name '*.bak-*' -o -name 'cao-window*' \)
```

- **You should see:** `diff` prints nothing and `find` prints nothing. Your main window works as before, and still shows what it showed in B9.
- **Broken means:** a new file, or a file named like the installer's.

### D3 The CAO terminal count is the same as before

- **Do:** on the CAO machine, `HC_SESSION=$CHK_CAO_CORE chk_hc ls`. Count the terminals. The sessions you started in Part C are still there until D4.
- **You should see:** N0 plus the terminals of the test sessions you made (each of C13 to C15, the supervisor, schedules' sessions). After D4 the count is N0 again.
- **Broken means:** fewer terminals than N0, which would mean the window or the uninstall killed one that was not yours.

### D4 Cleanup of test data on the CAO machine

**Guard built in.** The commands below remove sessions, schedules, and workflows. They run only when `CAO_API_PORT` and `CAO_HOME_DIR` name the scratch server of B0. Otherwise the block prints `STOP` and runs nothing. Edit the `cao shutdown` line first: name each session you made.

```sh
if [ "$CAO_API_PORT" = 9890 ] && [ "$CAO_HOME_DIR" = /tmp/cao-window-check-home ]; then
  cao shutdown --session <each session name you made, for example chk-sup and the cao-... ones>
  cao schedule remove chk-flow
  cao schedule remove chk-flow2
  cao schedule remove chk-flow3
  cao schedule remove chk-flow4
  cao schedule remove chk-sched
  cao schedule remove chk-race
  cao workflow delete chk-wf-py --yes
  cao workflow delete chk-wf-yaml --yes
  rm -r /tmp/cao-window-check
else
  echo "STOP: these variables do not name the scratch server (CAO_API_PORT=$CAO_API_PORT CAO_HOME_DIR=$CAO_HOME_DIR)"
fi
```

If it prints `STOP` (the case when you used the real server), nothing was removed. Remove what the checklist made by hand, one command at a time, and only the names you really created. Look at each list first, and run a command only for a name you see in it:

```sh
cao session list
cao schedule list
cao workflow list
cao profile list
```

Then remove. Every session the checklist starts has a `chk-` name, and the server stores it with a `cao-` prefix: `cao-chk-sup`, `cao-chk-block`, `cao-chk-mock`, `cao-chk-ask`, `cao-chk-auto`, `cao-chk-yolo`, `cao-chk-read`, `cao-chk-discard`, `cao-chk-adv`, `cao-chk-resume`, `cao-chk-ps`, and `cao-flow-chk-flow` (the one a schedule run makes). Do this for each that appears in `cao session list`:

```sh
cao shutdown --session <the session name from the list>
cao schedule remove chk-flow
cao schedule remove chk-flow2
cao schedule remove chk-flow3
cao schedule remove chk-flow4
cao schedule remove chk-sched
cao schedule remove chk-race
cao workflow delete chk-wf-py --yes
cao workflow delete chk-wf-yaml --yes
cao profile remove chk-draft-author --yes
cao profile remove chk-draft-nomcp --yes
cao profile remove chk-draft-two --yes
cao profile remove chk-draft-native --yes
cao profile remove chk-copy --yes
```

`cao profile remove` takes the profile name and deletes it from the local agent store (`cao profile remove --help` shows `--yes`). Never use `cao shutdown --all`. **Unconfirmed:** the exact session names and the output of `cao session list`: read the names from that list and from the window.

**The scratch core.** Skip this paragraph if you used the real server. Stop the scratch server with Ctrl+C in its terminal. Then, on the CAO host, check the core before you stop it. The core name is written out on purpose and is never taken from a variable:

```sh
HC_SESSION=caochk chk_hc server status
```

It must name the core `caochk`, and `HC_SESSION=caochk chk_hc ls` must list only your test terminals (or none). If either looks like your real CAO, stop here. Then:

```sh
HC_SESSION=caochk chk_hc server stop
rm -r /tmp/cao-window-check-home
```

This ends only the scratch core and the scratch home. The real core is named `cao`, and no command in this paragraph can reach it.

Remove your scratch homes: `rm -r ~/.config/herdr-app-cao-check.removed-<number>` for each one (look with `ls ~/.config`), and `rm /tmp/chk-manifest.json`. On the CAO machine name each session exactly as the window listed it, and remove only the schedules and workflows that you created. Never use `cao shutdown --all`: it would end your real sessions too. Skip the lines for anything you never created.

- **You should see:** each command succeeds. `HC_SESSION=$CHK_CAO_CORE chk_hc ls` shows N0 terminals again (run it before you stop the scratch core). `curl -s http://127.0.0.1:$CHK_PORT/sessions | grep -c 'cao-draft-'` prints `0` (run it before you stop the scratch server).
- **Broken means:** a test session or draft that will not go away.

### D5 Uninstall leaves a launcher it did not write

After D1, make a fresh scratch window (your old home is already renamed): run `chk_install; echo "exit=$?"` in the Mac terminal, as in B2. The installer's launcher has a header line: `# Written by examples/herdr-app-cao-window/install.sh. Re-run the installer to change it.` Uninstall deletes only a file that carries it.

```sh
echo '#!/bin/sh' > ~/.local/bin/"$CHK_LAUNCHER"
sh uninstall.sh --home "$CHK_HOME"; echo "exit=$?"
ls -l ~/.local/bin/"$CHK_LAUNCHER"
```

- **You should see:** `uninstall: note: left <path> in place: it does not carry the header line install.sh writes, so it is not the launcher it wrote`, then `uninstall: done. ...` and `exit=0`. The file is still there. The same note appears for a symbolic link (`it is a symbolic link`) and for a folder (`it is not a file`).
- Remove the file by hand afterwards: `rm ~/.local/bin/herdr-cao-check`. The second uninstall renamed that home to another `herdr-app-cao-check.removed-<number>`; D4 removes both.
- **Broken means:** your own file is deleted, or uninstall fails because of it.

### D6 Uninstall refuses a bad manifest

Run this before D1. The last command puts the manifest back. Uninstall reads the manifest again as the installer would accept it.

```sh
cp "$CHK_HOME/cao-window-install.json" /tmp/chk-manifest.json
jq '.launcher = "a/b"' /tmp/chk-manifest.json > "$CHK_HOME/cao-window-install.json"
sh uninstall.sh --home "$CHK_HOME"; echo "exit=$?"
jq '.core = "-x"' /tmp/chk-manifest.json > "$CHK_HOME/cao-window-install.json"
sh uninstall.sh --home "$CHK_HOME"; echo "exit=$?"
cp /tmp/chk-manifest.json "$CHK_HOME/cao-window-install.json"
```

- **You should see:** the first run prints `uninstall: the manifest's launcher value is not a file name; nothing was changed`. The second prints `uninstall: the manifest's core value is not a name install.sh would accept (letters, digits, dot, dash or underscore, no leading dash); nothing was changed`. Both end with `exit=2`. The window's home, the machine entry, and the core are as before (`HC_SESSION="$CHK_CORE" chk_hc machine ls` still lists `cao`).
- **Broken means:** any other exit code, or a changed core, machine, or launcher.

## Where each task item lives

| Task 15.2 item | Line |
|---|---|
| Install into a fresh home | B0, B2 to B6 |
| The launcher opens a second Dock tile | C01 |
| A repeat launch makes no second process and raises the window | C02 |
| The main window is unchanged and the CAO terminal count is the same | C03, D3 |
| Only CAO spaces show | C04 |
| The two left blocks fold and keep their state over a restart | C05 |
| Keyboard: Up, Down, Left, Right, Enter, and P in Sessions | C06 |
| A session shows its whole tree, however many agents, and the block scrolls | C07 |
| A pin survives a restart | C08 |
| Agent tabs with a pinned worker | C09 |
| A blocked worker marks its session and counts in the bar, and the list keeps its order by age | C10 |
| A replied conductor clears on focus | C11 |
| The hand-off toast shows a held ask, and Later re-holds it | C12 (the hold and Later are not built: n/a) |
| New session with Ask first, Auto-approve, and YOLO | C13, C14, C15 |
| Env rows show the command instead of launching | C16 |
| Create a schedule disabled, enable it, run it, and edit its schedule | C17, C18, C19, C20 |
| A workflow's first run shows plan approval needed | C21 |
| Draft with AI fills a form and leaves no author session behind | C22, C36 |
| Stopping cao-server shows the unreachable state | C23 |
| `uninstall.sh` leaves the main home untouched | D1, D2 |
| macOS run of `install.test.mjs` | B1 |

Lines for later fixes:

| Behavior | Line |
|---|---|
| Draft needs an author profile with exactly one MCP server, no `native_agent`, and a restrictable provider | C22 (a good profile), C39 (refusals) |
| No schedule Create without `POST /flows/validate`; the form shows `cao schedule add` and `cao schedule disable` | C38 |
| An allowed-tools override with `*` is YOLO; a broad named list is not | C37 |
| Request bodies are visible in `ps`; env values are never sent | C41, C16 |
| Uninstall removes only the launcher it wrote; it refuses a bad manifest | D5, D6 |
| The window's home is mode 700 after every install | B10 |
| A pre-existing core named by `--core-session` is stopped by uninstall | Before you start (`CHK_CORE`) |
| Schedule limits: 256 characters, "this schedule never fires", names for weekdays | C40, C17 |
| Workflow names (`runs`, over 64 characters), Python edit with the hash, YAML `name:` match | C31, C32, C33 |
| A profile named `search`, `templates`, or `schema` cannot be read | C29 |
| Stopping, freezing, or editing a profile never touches the real server: a scratch CAO server | B0, C23, C29, C34, C35 |
| The checklist's `hc` calls clear the `HC_*` variables | Before you start (`chk_hc`), B3, B9, U09, D6 |
| Two windows write one CAO structure document; the updater | U19 |
| Known gap note in the status bar help, best guess, "stuck?" | C42, C55 |
| Workflow Cancel and Resume confirmations | C43 |
| Schedule... from a workflow and "Scheduled by" | C44 |
| The 409 on a duplicate schedule name | C45 |
| Launch Advanced fields (`--memory`, an allowed-tools override), and no engine or resume field | C46 |
| A way to get a blocked agent | C0, C10 |
| One line per session: its letter badge, name, marks and state mark at the right end, no state word, the narrow width | C47, C62 |
| The `+` of the Sessions header opens the New session dialog; no row at the end of the list | C48 |
| The Agents tree: one line per agent with its program logo, name, age, state mark and pin; its indentation | C49, U25 |
| The Agents block highlights the focused terminal | C67 |
| The window's OS title is `CAO`; the main window keeps `Herdr` | C68 |
| The top-left of the sidebar shows the CAO mark and says `CAO`, with the search and fold buttons | C69 |
| An agent that waits on its workers shows the muted spinner and "waiting on N workers", not done or Replied | C70 |
| Cmd-Tab, the Dock and Mission Control say CAO with the CAO icon (the app copy), Herdr's own window says Herdr; Keep in Dock; a rerun is up to date | C71 |
| The filter header and its clear control | C50 |
| "Started by schedule X" opens the right sidebar at that schedule | C51 |
| The top bar with one shown agent shows its one tab; its two bare-word buttons | C52, C53, U26 |
| The status bar parts as buttons; the `?` help and its two notes | C54, C55 |
| The right sidebar: list, details, back arrow, collapsed prompt | C56, C57 |
| An answer after leaving the right sidebar's details; a hidden copy keeps receiving answers | C65, U27 |
| The right sidebar is hidden at every start; show and hide by id | C64, U24 |
| The dialog: size, first read, closing keys, Esc with edits, Create and enable | C58, C59, C60, U20 |
| The dialog's open state is a lease | C63 |
| A closed Draft with AI dialog leaves no author session | C61 |
| A workflow Run in the dialog, plan approval and nothing typed stored | C66 |
| Esc and Tab order in the dialog (the key test, not run) | U21, U22, U23 |
| The theme a copied settings file names | U28 |
| The left sidebar's edge drags between 200 and 640 px | U29 |

## Report back

Send the filled results table, the notes for each FAIL, the "Report back" text for every U line, and these facts:

```sh
sw_vers -productVersion
defaults read /Applications/Herdr.app/Contents/Info CFBundleShortVersionString
hc --version
git rev-parse --short HEAD
```
