# Keyless e2e

`npm run e2e` runs `run.sh`, which starts a private `cao-server` and then runs
`e2e.ts`. `e2e.ts` drives the app's `core/` headless, through the same
`createController()` the view uses. It needs tmux, Node.js 22.18 or later, and a
CAO checkout with `uv sync --all-extras`. It needs no provider login and no API
key.

## What is simulated, and what is real

**Simulated: an agent showing a permission prompt.** The harness types the
marker `APPROVAL_REQUIRED: <text>` into a `mock_cli` pane **without Enter**,
standing in for a CLI that prints a prompt and waits for an answer. The server
runs with `CAO_MOCK_CLI_SCRIPTED_PROMPTS=1`, so `mock_cli`'s provider reports
that pane as `WAITING_USER_ANSWER`.

Without Enter is the point. Sent through `POST /terminals/{id}/input`, the marker
would be submitted as a line: `mock_cli` echoes it, prints a fresh prompt, and
the provider reads the text after the marker as an answer, so the terminal never
waits.

The harness also calls `POST /agui/v1/emit_ui`, acting as an agent.

**Covered for real**, all in the running `cao-server`:
- the approval bridge picking up `WAITING_USER_ANSWER`;
- classification into an interrupt and its `responseSchema`;
- the run plane: `RUN_FINISHED(interrupt)`, `resume[]`, and its outcome;
- key delivery: the pane receives `y` for approve and `n` for deny;
- `emit_ui` validation and the ambient stream.

## What it checks

- An empty run: `RUN_FINISHED(success)`, no cards.
- Approve and deny, one interrupt each.
- Edit refused in the client where the prompt does not offer it: no run is
  sent, and the agent is still waiting.
- Two interrupts at once, from two `mock_cli` terminals prompted the same way:
  nothing is sent until both are decided, then one `resume[]` covers both.
- The six components from real `emit_ui` calls, read off the ambient stream
  on both transports at once: `EventSource` on `GET`, and fetch on `POST` with a
  token (auth is off on this server, so any token is accepted; the point is the
  `POST` path).
- An off-list `emit_ui` call: HTTP 400, and nothing reaches either stream within
  a 2 s settle window.
- The app requests no route outside `core/routes.ts`.

## Isolation

The harness resumes whatever interrupt the run plane reports, so it only ever
talks to the server `run.sh` starts:
- port 9896 by default (`CAO_E2E_PORT`), refusing 9889 and any port that
  already answers, and failing at once if the server process exits before it is
  healthy;
- a throwaway `HOME`, so it never opens your `~/.aws` CAO database;
- its own tmux server: `TMUX` and `TMUX_PANE` removed and a fresh
  `TMUX_TMPDIR`.

tmux is run in exactly one place per file: `private_tmux` in `run.sh` and
`privateTmux` in `e2e.ts`. Each always names the private socket with `-S` and
runs with `TMUX` and `TMUX_PANE` removed. In `run.sh` the socket path is built
from the scratch directory the script itself created, so it can only name this
run's server. `e2e.ts` receives the path from its environment, so it checks it:
it refuses to run tmux unless `TMUX_TMPDIR` is set, is not the shared `/tmp`, and
contains the socket. That check holds when the script is run through `run.sh`,
which sets `TMUX_TMPDIR` to its fresh scratch directory. Nothing in the harness
uses a bare `tmux` command or `-L`.

`cao-server` itself reaches tmux through libtmux's default socket. It is
contained the same way: `run.sh` starts it with `TMUX` and `TMUX_PANE` removed
and `TMUX_TMPDIR` set to the private directory, so its default socket is the
private one.
The server is stopped with `-S <socket> kill-server`, and the scratch directory
is removed.
