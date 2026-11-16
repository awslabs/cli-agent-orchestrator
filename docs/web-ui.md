# Web UI

CAO includes a web dashboard for managing agents, terminals, and flows from the browser.

![CAO Web UI](https://github.com/user-attachments/assets/e7db9261-62b1-4422-b9f5-6fe5f65bdea4)

## When you need Node.js

The pre-built Web UI is bundled inside the CAO wheel (at `src/cli_agent_orchestrator/web_ui/`), so a regular `uv tool install` ships everything you need. **You do not need Node.js or `npm install` to use the Web UI.**

Node.js 22.22.2 or later is only required if you want to:

- Run the frontend dev server for hot-reload development (Option A below), or
- Rebuild the bundle from source.

Install Node only if one of those applies:

```bash
# macOS (Homebrew)
brew install node

# Ubuntu/Debian
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo bash -
sudo apt-get install -y nodejs

# Amazon Linux 2023 / Fedora
sudo dnf install nodejs22

# Verify
node --version   # 22.22.2 or higher
```

## Starting the Web UI

### Option A: Development mode (hot-reload, two terminals)

```bash
# Terminal 1 — start the backend server
cao-server

# Terminal 2 — start the frontend dev server
cd web/
npm install        # First time only
npm run dev        # Starts on http://localhost:5173
```

Open http://localhost:5173 in your browser.

### Option B: Production mode (single server, no Vite needed)

The built Web UI is bundled into the CAO wheel, so a plain `uv tool install` ships everything you need. Just start the server:

```bash
cao-server
```

Open http://localhost:9889 in your browser.

To rebuild the frontend from source:

```bash
cd web/
npm install && npm run build   # Outputs to src/cli_agent_orchestrator/web_ui/
uv tool install . --reinstall
```

> **Custom host/port:** `cao-server --host 0.0.0.0 --port 9889` exposes the
> server to the network. Review the
> [network configuration and PTY WebSocket guidance](configuration.md#network-network--env-var-only)
> before doing this.

## Remote machine access

If you are running CAO on a remote host (e.g. a dev desktop), forward the ports over SSH:

```bash
# Dev mode (proxy both frontend and backend)
ssh -L 5173:localhost:5173 -L 9889:localhost:9889 your-remote-host

# Production mode (backend serves UI directly)
ssh -L 9889:localhost:9889 your-remote-host
```

Then open the same URLs (localhost:5173 or localhost:9889) in your local browser.

## Features

Manage sessions, spawn agents, browse and author agent profiles (create from templates or from scratch, edit, clone, delete — with server-side validation before every write), create scheduled flows, configure agent directories, and interact with live terminals — all from the browser. Includes live status badges, an inbox for agent-to-agent messaging, output viewer, and provider auto-detection.

### Edit agent descriptions

In Home or Agents, use the pencil beside an agent's description to edit it.
Choose Save or press Enter to save; choose Cancel or press Escape to discard
the draft. Save an empty or whitespace-only value to clear the description.
Descriptions persist across reloads. Polling updates and transient read failures
preserve an open draft; deleting the selected session closes its detail panel
when the next poll returns `404`.

The editor normalizes line separators to spaces, removes C0/C1 controls
(including interior tabs), and trims outer whitespace before validating the
64 Unicode code point limit. Overlong edits show an error and keep the draft.
See the [terminal description API contract](api.md#terminal-descriptions) for
creation defaults, clearing, and retry behavior.

### Workflows

The Workflows tab lists the stored workflows. Pick one to validate it, run it,
and follow its runs. New workflow opens the workflow builder, which makes a
YAML workflow step by step: pick an agent profile for each step, fill in the
params the profile declares, and write the prompt. A YAML workflow also gets
Edit and Delete. The CLI and the MCP tools write Python workflows only, so the
builder and the HTTP routes are the only ways to write YAML. See
[CAO Workflows](workflows.md).

To clone an existing workflow, pick it under Clone from at the top of New
workflow. Clone from shows only when a YAML workflow is stored, and it lists
the YAML workflows by name. The form fills with the description, inputs and
steps of the one you pick, under a free name such as `nightly-check-copy`, and
saving creates a new workflow, leaving the original unchanged. When the form
already has content, the builder asks before it replaces it.

The YAML beside the form follows the form, and you can also edit it directly,
in New workflow and in Edit. Once you edit the YAML by hand, the form and the
flow preview are paused, and Validate and Save use the YAML as written,
comments included. Discard YAML edits gives the form back. Save always
validates the YAML on the server first, and saves nothing when validation
fails.

A note at the top of the builder says what a YAML workflow cannot do: branch,
loop, run steps in parallel, pass one step's answer to a later step, or run
`assign` and `send_message` steps. For those, write a Python workflow. You can
still pick and save `assign` and `send_message`. The step editor marks them
"not built yet", the notice after a save says a run is refused until they are
built, and the run routes refuse them.

The builder opens only a YAML workflow it can save back without losing data.
For any other workflow it shows the file's path and the fields that block
editing. Edit that file in a text editor instead. The Workflows tab reads the
workflow files again each time it lists them, so reopen the tab to see the
change, and use Validate to check it.

The step editor warns when a param value starts or ends with a space, such as
"This value starts with a space." It does not trim the value and does not
block saving, because the space can be what you meant.

When you edit a workflow that has runs, the builder says that changes apply to
new runs only. Runs that already started, including runs you resume, keep the
version they started with. Resume reads the copy of the workflow saved with the
run, not the file.

#### Following a run

The Workflows tab opens on its home view. The left column holds New workflow,
"Run a workflow" and "Approve a plan id". The main column lists the runs, newest
first. Each row shows the run's state, the workflow, a short run id, when the run
started, how long it ran and how far it got, such as "2 of 3" for a run on its
second of three steps. Select a run to open its run page. A run you start from
"Run a workflow" opens its run page too.

The run page fills the window below the tab row. The Workflows button at its top
left goes back to the home view. Next to it are the workflow's name and the run's
state. While the run runs, the state's icon spins and shows how long the run has
been running, and a hint says Live or Reconnecting. A finished run says
"Completed in 1 min 12 s", Failed or Cancelled. On the right are Cancel while the
run is live, Resume for a failed or cancelled run, and More, with Export
diagnostics, Compare runs and Delete run. When Resume needs a new approval,
because the workflow changed since its plan was approved, a prompt under the bar
offers Approve & resume. A failed run shows its failure detail there too: the last
step, the failing attempt, the error kind and the terminal.

The left column lists the steps in run order, with how far the run got, such as
"Steps · 2 of 3". The line between them is green below a finished step and blue
below the running one. Each step shows its state, its name, how long it took, and
its agent and provider. A finished step shows the first two lines of its answer.
The running step spins and counts its time, such as "0:48", and shows its attempt,
such as "Attempt 1 of 2", and the names of its params. A step that has not started
says which step it waits for. A failed step shows its error kind and the first
line of its error. If your system asks for reduced motion, the icons stay still,
and the text still says what each step is doing. Once the run has ended, a last
node, Result, shows the run's result, its report files and the run comparison.
Under the steps are when the run started, how long it ran, its run id and its
tier.

The console on the right shows the selected step. While the run is live, "Follow
the running step" is on: the console shows the running step and moves to the next
one when it starts. Selecting another step turns Follow off, and turning it on
again goes back to the running step. A run that has ended opens on the step that
failed, or else on its last step with an answer. The console has four tabs, and
Copy copies the text of the tab you are on:

- **Terminal** shows the step's terminal as plain text. While the step runs, the
  tab shows "Live", refreshes once a second and says how many seconds ago it last
  updated. It pauses while the browser tab is hidden. It is a read-only capture of
  the screen, marked "Watch only", so it cannot type into the agent's terminal or
  resize it. The view stays at the newest output and says "Following the output".
  When you scroll up to read, it stays where you are and offers "Jump to latest".
  After the step ends, the tab shows the step's saved transcript instead. CAO saves
  the transcript when the terminal closes, and deletes it with the terminal's other
  files after 14 days (`RETENTION_DAYS`). A step that timed out or crashed keeps its
  terminal open, so until that terminal ends, the tab shows its screen as "Terminal
  still open". When there is no transcript and no open terminal, the tab says "No
  transcript was saved for this step."
- **Output** shows the step's answer: its structured output, pretty-printed, when
  the step has one, and otherwise the agent's final answer. A badge says when the
  answer was truncated or had secrets redacted. A running step shows "Still
  running". A step that finished with neither, such as a step of a run from an older
  CAO version, shows "No answer was recorded".
- **Params** shows the step's params with its agent, provider and call type. They
  come from the current workflow file, so a file edited after the run started shows
  its new values, and the tab says so.
- **Events** lists the step's events with plain labels.

A running step opens on its Terminal tab, and a finished step on its Output tab.
The Result node shows the last answer the same way: the output of the last step
that has one, with that step's name, so a run that halted before its last step
still shows what its steps said.

The row under the console shows the run's three newest events. "All N events"
opens the full timeline in a panel on the right, and selecting one of the three
events opens it on that event. The timeline lists the run's events in order. Each
row shows the local time, the step and a plain label, such as "Attempt 2 failed
(timeout)" or "Step completed in 29 s". Only an event that records an outcome has
an outcome badge. While the run is live, the selection follows the newest event
until you select one. Latest jumps back to the newest event and follows again. The
pane under the timeline shows the selected event's step through the same live view
and transcript, and says so when an event belongs to the run rather than to a step.
When the journal declared events missing, the row under the console says how many
gaps there are, and the timeline marks each one.

## Related

- [web/README.md](../web/README.md) — frontend architecture and component details
- [docs/configuration.md](configuration.md) — agent directory configuration
- [docs/control-planes.md](control-planes.md) — where the Web UI fits alongside `cao session` and `cao-ops-mcp`
