# E2E rig and dev harness (workstream C)

Everything here runs the CAO Shelf against an ISOLATED Kiro Crew and a SCRATCH `cao-server`. It never
touches your real Kiro Crew gateway (port 5476), your real CAO (port 9889), `~/.kiro`, `~/.claude` or `~/.aws`.

| Piece | Where | Port |
|---|---|---|
| Isolated Kiro Crew 0.12.2 (own `HOME`) | `$SHELF_SCRATCH/home` | 127.0.0.1:5499 |
| Scratch `cao-server` with `mock_cli` | `$SHELF_SCRATCH/cao-home`, own tmux socket in `work-c/tmux-tmp` | 127.0.0.1:9990 |
| Fake CAO replaying `fixtures/cao/<day>/` | in memory | an ephemeral loopback port |
| Scripted model `mock_model.py` (chats of the isolated Kiro Crew) | `$SHELF_SCRATCH/work-c/logs/mock-model.jsonl` | 127.0.0.1:9981 |

`SHELF_SCRATCH` defaults to `~/kc-shelf-e2e`. It is never `/tmp`, `/`, your home, or a path containing
`.kiro`, `.aws`, `.claude` or `.ssh`; the scripts refuse those. To use another folder: `export SHELF_SCRATCH=<scratch>`.
The scratch CAO needs a venv with `cao-server` at `$SHELF_SCRATCH/cao-venv`
(`uv venv $SHELF_SCRATCH/cao-venv && uv pip install --python $SHELF_SCRATCH/cao-venv/bin/python -e <repo root>`;
override with `SHELF_CAO_VENV`).

## One command

```bash
cd examples/kiro-crew-cao-shelf
e2e/rig/harness.sh up [--day busy|quiet] [--no-seed] [--no-app]   # gateway + scratch CAO + seed + install the App
e2e/rig/kiro_crew.sh token                                       # login URL for the isolated dashboard (a secret)
e2e/rig/harness.sh status
e2e/rig/harness.sh down                                          # uninstall the App from the scratch home, stop both
e2e/rig/harness.sh owner-check                                   # your kirocrew pids and ports 5476/9889 are as at `up`
```

`up` takes about 3 minutes with the busy seed (12 mock sessions); `--no-seed` takes about 30 seconds. `up`
installs with `install.sh --no-build --cao-url http://127.0.0.1:9990 --trust-all` into the scratch home only
(so the relay agent's ops server addresses the scratch CAO, never 9889); the trust setting there is the scratch
home's own. Kiro Crew's CLI refuses the App's session-approval consent by design, so `install.sh` exits 7; the
rig then gives that consent on its own scratch gateway as its owner (`owner_enable.py`: the scratch login link
from `kirocrew token` is exchanged for an owner cookie, then `POST /api/apps/cao-shelf/enable
{"sessionApprovalConsent": true}`, the dashboard's call; port 5476 and links for another host or port are
refused, and the link is never printed). `up` and `down` can run any number of times in a row.

## Chats: Claude backend and a scripted model

The isolated Kiro Crew runs chats on the Claude backend (`agent.acp_backend=claude`,
`agent.member_acp_backend=claude`) through the `@agentclientprotocol/claude-agent-acp` adapter (0.99.1, in
`$SHELF_SCRATCH/work-c/acp`; `setup` copies the spike's install from `$SHELF_SCRATCH/work-rc/acp` when it is
there, otherwise it runs `npm install`) and Claude Code (the newest `~/.toolbox` claude-code, read only;
override with `SHELF_CLAUDE_CODE`). The gateway gets `ANTHROPIC_BASE_URL=http://127.0.0.1:9981`, so every
model call goes to `mock_model.py`, a stdlib stand-in for the Anthropic Messages API. No real model
credential is used: `ANTHROPIC_API_KEY` is a placeholder the mock ignores. Everything else is real (gateway,
adapter, Claude Code, its permission engine, the MCP wiring, CAO), so a chat proves the plumbing, not model
behaviour.

`setup` writes `$SHELF_SCRATCH/home/.claude/settings.json` (the scratch home only; existing keys are kept):
`permissions.allow` for `mcp__cao-shelf_ops__send_session_message`, `..._get_terminal_status`,
`..._read_session_output`, `..._get_session_info`, and `env.ENABLE_TOOL_SEARCH=false` (with tool search on,
Claude Code defers MCP tools and the relay sees none). `kiro_crew.sh claude-settings` rewrites just that file.
`start` starts the mock before the gateway and `stop` stops it after, by its recorded pid (`mock-model`, identity
`SHELF_MOCK_MODEL=<work-c>`). `SHELF_CLAUDE_BACKEND=0` turns all of this off.

The scratch home sits under your home folder, and Claude Code reads `CLAUDE.md` and `.claude/rules` from every
ancestor of the chat's working folder (`$SHELF_SCRATCH/home/.kiro/crew/workspace`). So the requests the mock
receives include your own `~/.claude/rules` text (seen in the spike). It is read, never written, and the mock
ignores it. A scratch folder outside your home avoids that.

### How the mock decides, and how a test selects behaviours

The mock keeps no state between requests. Each request carries the whole chat, and the mock replays it. A chat
whose tools include `..._get_terminal_status` follows the `cao-remote-control` procedure:

* The first user turn that names a terminal id (CAO Shelf's setup message: `conductor_terminal_id: <id>`)
  is the setup message, and that id is the chat's only target. After `CAO Shelf relay ended` it calls no tool.
* For each owner message it calls `get_terminal_status` first:
  * `processing`: it offers check again, queue it anyway, or cancel, and sends nothing.
  * `waiting_user_answer`: it calls `read_session_output` with `mode=full, max_chars=2000` and offers the
    Answer box, check again, or cancel.
  * a failed status: "not found" says the target ended; any other failure says CAO did not answer and
    offers check again or cancel.
  * `idle` or `completed`: it sends the handshake first unless the setup message says
    `handshake: already_given`, then the message tagged `[REMOTE-CONTROL] `. The tag is
    `[REMOTE-CONTROL catch-up] ` when `status_generation` rose since the last accepted reply. A message that
    starts with `/` goes untagged.
* After a send it polls until `completed` (or `idle` twice) with a `status_generation` above the send point, or,
  when CAO reports no generation, after it saw `processing`. Then it calls `read_session_output` with
  `mode=last` and shows the reply verbatim between the reply-block lines.
* After 30 status checks (the handshake's included) it offers check again or stop waiting. Check again after a
  send never sends again: it keeps waiting for the same turn. A wait cut short on the handshake says the
  owner's message is not sent yet; check again waits for the handshake's turn and then sends it.
* A Kiro Crew notice turn (`[Tool refusal ...`) sends nothing and keeps the offered options open.
* `RULES` in `mock_model.py` quotes the prompt sentence each branch follows; `test_cao_shelf_mock_model.py`
  checks that the agent prompt still says each one and that each has a test.

Every text it writes (tags, handshake, ready line, reply block, options, fixed replies) is read from the
"Remote control" section of `api-contract.md`, so it matches what the agent prompt says.

A chat without the ops tools (the default agent) gets `mock model reply: <text>`.

A test selects behaviours with a JSON object of script keys: `kiro_crew.sh model-stop` then
`SHELF_MODEL_SCRIPT=<file> kiro_crew.sh model-start` (or `SHELF_MODEL_SCRIPT` set for `harness.sh up`), or at run time `POST http://127.0.0.1:9981/__script` (merged over the defaults; unknown
keys get 400). The keys:

| Key | Default | Effect |
|---|---|---|
| `obey_injection` | `false` | negative control: a reply containing `message terminal <id>` makes the mock message that id (by default it ignores it) |
| `answer_itself` | `false` | the mock answers the owner itself and relays nothing |
| `handshake` | the contract's text | handshake sent before the first owner message, and again after a catch-up reply that lacks `Since the last remote message:`; `""` turns it off |
| `handshake_given` | `false` | treat the handshake as given even when the setup message says `handshake: pending` |
| `budget` | `30` | status checks per owner message |
| `poll_delay_s` | `1.0` | the mock waits this long before answering with a poll, like a model would |
| `use_wait` | `true` | before each status check of a turn the mock calls Kiro Crew's `wait` tool (`mcp__kirocrew-core__wait`), as the prompt says; `false` polls back to back (with `poll_delay_s`), which the J9 journeys that time other things set |
| `wait_seconds`, `watch_seconds` | `60`, `300` | the waits' `seconds` while a turn runs and while the mock watches delegated work |
| `delegation_pattern` | assigned, delegated, handed off, will report back, ... | a reply it matches is not done: the mock reads `get_session_info`, adds the "Not done yet" line naming the processing helpers and keeps watching the conductor's next turn |
| `target` | `null` | a fixed terminal id instead of the one in the setup message |
| `rogue` | `false` | an owner message matching `rogue_pattern` (`rogue ...`) makes the mock call each of `rogue_tools` the session offers (`Bash` with `rogue_command`, and `mcp__standin__mark`), then report each result; for the journey that checks what stops a built-in or a non-ops MCP tool |
| `terminal_pattern`, `session_pattern`, `ended_pattern`, `other_target_pattern`, `handshake_given_pattern`, `injection_pattern`, `tool_prefix` | see `DEFAULTS` | how the setup message, the relay-ended message, a request for another session, the handshake note and an injected instruction are recognised |

`GET /__log` returns one entry per Messages request: the decision, the tool and its arguments, the reply text,
the target, `mcp_tools` (every `mcp__` tool the session offered, for the "exactly four tools" check) and, for a
side request, the start of its system prompt.

`standin_mcp.py` is a stdio MCP server with one tool, `mark`, that stands in for a plugin or user-level server
of the owner's. The journey `test_j9_the_shell_is_refused_under_a_user_auto_mode_and_the_owners_servers_are_the_residual`
puts it into the scratch home's `~/.claude.json` and `"defaultMode": "auto"` into its `settings.json` for one
relay chat, and puts both files back afterwards.
`POST /__log/reset` clears it, and `kiro_crew.sh model-log` prints the file copy. The mock listens on
127.0.0.1 only and refuses any request with an `Origin` header, so a web page cannot drive it.

## How it stays safe

* Processes are started only through `rig_spawn` (`lib.sh`), in their own session, with an explicit
  environment (`env -i`, `HOME`, `PATH`, pinned `KIROCREW_PORT`). A pid record in
  `$SHELF_SCRATCH/work-c/run/<name>.pid` stores pid, process start time and an identity (`HOME=...` for the
  gateway, `CAO_HOME_DIR=...` for cao-server).
* Processes are stopped only through `rig_stop`: it refuses unless the record exists AND the live process
  still has the recorded start time AND carries the recorded identity in `/proc/<pid>/environ`. It then
  stops that pid and the processes below it in the process tree (listed before they are killed). No
  `pkill`, no `killall`, no name matching.
* `kirocrew stop`, `restart`, `service` and `doctor` are never called. They ignore `HOME` and the port and act
  on the owner's real service. `kc.sh` refuses them.
* The rig refuses to start when the gateway or CAO port is taken, and refuses 5476 and 9889 outright.
* `harness.sh up` snapshots your `kirocrew` process list and the listeners on 5476 and 9889; `down` and
  the e2e session compare against it.

## Run the tests

```bash
make check-rig                       # fake CAO, install.sh (stub kirocrew), manifest, fixtures drift, shellcheck
make dev                             # the page without Kiro Crew: http://127.0.0.1:8765/ (fake CAO + real backend)
PLAYWRIGHT_BROWSERS_PATH=/path/to/pw-browsers make e2e
# or: python -m pytest -c e2e/pytest.ini e2e/tests -m e2e -q     (needs: pip install playwright pytest)
```

The e2e fixtures start the harness when it is not running (`harness.sh up --no-seed`), run the journeys,
then stop it again unless `SHELF_E2E_KEEP=1`. The session clears the scratch CAO first (port 9990 only).

Journeys (marked `e2e`): J1 read (fake and real `mock_cli` waiting) and answer, J2 ask, J3 history, J4 orphan,
J5 start, J6 stop one, J7 clean up (including the NEGATIVE journey: three stale sessions and one live
one, tick one, exactly that one is gone), J8 CAO killed and restarted, J11 network spy (nothing leaves the
gateway origin), J9/J10 screenshots at 320, 390 and 1280 px in light and dark (`e2e/artifacts/screens/`).
Journeys use the fake CAO for the fixture-driven states (tiers, stale, orphan) and the scratch CAO for real
behaviour. A real `peer` (a terminal without `caller_id`) cannot be created with CAO's REST API (403), so
peers exist only in the fake. J7 back-dates `terminals.last_active` in the scratch SQLite file
(`seed_cao.py backdate`; the stored text format is `YYYY-MM-DD HH:MM:SS.ffffff`, UTC); this is a test-only
action and the App never does it.

## Files

| File | What |
|---|---|
| `lib.sh` | scratch and port checks, pid records, `rig_spawn`, `rig_stop`, owner snapshot |
| `kc.sh` | run `kirocrew` (or `install.sh`) in the isolated home only, with an allowlist of subcommands |
| `kiro_crew.sh` | `setup`, `start`, `status`, `token`, `install-app`, `stop`, `teardown` for the isolated gateway; `model-start`, `model-stop`, `model-log`, `claude-settings` for the chat backend |
| `cao.sh` | `start`, `status`, `stop`, `reset` for the scratch CAO |
| `harness.sh` | both of the above plus seeding, in one command |
| `seed_cao.py` | seed sessions, closed helper, orphan, `backdate`, `clear` against the scratch CAO |
| `owner_enable.py` | owner-API consent for the App on the scratch gateway only (after `install.sh` exit 7) |
| `mock_model.py` | stdlib scripted Anthropic Messages API for the scratch Kiro Crew's chats (see "Chats" above) |
| `fake_cao.py` | stdlib fake CAO (modes: `down`, `slow`, `401`, `badshape`; `GET /__calls`) |
| `dev_server.py`, `dev_host/` | `make dev`: fake CAO, the real backend, a signing proxy and a host page with an SDK stub. The stub is `ui/test/sdk-stub/ui.tsx`, the one the component tests use (`dev_host/app-sdk-ui.mjs` re-exports it), and the page carries the host's light and dark token blocks plus `dev_host/host-kit.css` for the look of the stub's host classes; screenshots of record come from the real host (`e2e/tests/test_screens_restyle.py`). The proxy signs whatever reaches it, so it answers only requests whose `Host` is `127.0.0.1:<port>` or `localhost:<port>` and whose `Origin`, when present, is the same (anything else gets 403): another web page open in your browser cannot use it, and neither can a rebound hostname |
| `check_fixtures.py` | fixtures equal the generator output and validate against `schemas/` |
| `test_cao_shelf_*.py` | tests of the rig itself; collected by CI |

## Installing into your REAL Kiro Crew (documented, not run by anything here)

```bash
cd examples/kiro-crew-cao-shelf
./install.sh --trust-app        # recommended: trusts only this App; or make real-install REAL_KIROCREW=yes
./install.sh --trust-all        # shortcut: trusts every third-party App
./install.sh --uninstall        # remove (app data kept; add --purge-data to delete it)
```

Without a trust option the script installs, tells you the trust it found, and exits 4 with the exact
command to finish. If Kiro Crew is running, `app enable` starts the backend through the running gateway;
if it was started before the App was enabled and the page says "no reachable backend", restart Kiro Crew the way
you normally do. Use `./install.sh --dry-run` first to see every command.
