# Kiro Crew

[Kiro Crew](https://github.com/kirodotdev/KiroCrew) runs a gateway on your
machine that serves a dashboard: in a browser, in the desktop app, or on a
phone through a tunnel. In the dashboard you chat with agents, which run on an
ACP backend such as `kas` or `claude`, and you add Apps, packages that bring
their own backend, page, and agents into Kiro Crew. This page shows you how to
use CAO from Kiro Crew.

## What CAO offers Kiro Crew

- **The CAO Shelf App.** A page in the Kiro Crew sidebar that shows every CAO
  session as one card, sorted into Needs you, Live, Parked, and Stale. From a
  card you see the conductor and its helpers, ask the conductor something,
  answer a waiting helper, start a session, and stop sessions after a
  confirmation dialog that lists them, with none ticked by default. It lives in the CAO repository as an example:
  [`examples/kiro-crew-cao-shelf/`](../examples/kiro-crew-cao-shelf/README.md).
- **Remote control.** From a card, you open a Kiro Crew chat tied to that one
  CAO session. Your messages go to the session's conductor and its replies come
  back to the chat, so you can steer a session from your phone.
- **`cao-ops-mcp`.** CAO's operations MCP server. The relay chat uses four of
  its tools. Any MCP client can run it to launch, inspect, message, and stop
  sessions; see [the `cao-ops-mcp` server](control-planes.md#cao-ops-mcp-server).

The CAO Shelf is not a Kiro Crew built-in and not part of the `cao` package.
Its backend talks to CAO only through the [HTTP API](api.md) on loopback, and
CAO stays the source of truth.

## Requirements

| Item | What you need |
|---|---|
| Kiro Crew | `kirocrew` on `PATH`, version 0.12.2 or later. 0.12.2 is the only version tested. |
| CAO | `cao-server` running on loopback, by default at `http://127.0.0.1:9889`, with auth off (the default). |
| `cao-ops-mcp-server` | On `PATH` when you install. It comes with CAO; the installer refuses to install without it. |
| OS | macOS or Linux. |
| Node | 22.12 or later, only to rebuild the page after you edit its sources. The built page is committed with a content hash of its inputs, so a clone installs without Node. |

The Kiro Crew dashboard must be served from the origin root, as it is by
default.

## Install the CAO Shelf

Run these steps from a clone of this repository.

1. Start CAO, if it is not running. Run it in its own terminal and leave it
   running there, since it runs in the foreground; run the later steps in
   another shell:

   ```bash
   cao-server
   ```

2. Choose how you trust the App. Kiro Crew refuses to run third-party App code
   until you allow it, and enabling an App runs its backend with your user
   account's privileges. Pick one option:

   - `--trust-app` trusts only `cao-shelf`. This is the recommended option.
   - `--trust-all` sets `agent.apps_allow_third_party=true`, which trusts
     every third-party App, not only this one.

   Read [the trust step](../examples/kiro-crew-cao-shelf/README.md#the-trust-step-in-plain-words)
   before you choose.

3. Run the installer:

   ```bash
   examples/kiro-crew-cao-shelf/install.sh --trust-app
   ```

   The first line it prints, `Target:`, names the `HOME` and gateway port it
   acts on. To rehearse first without touching your Kiro Crew, add
   `--dry-run`. Two options change the relay chat:

   - `--cao-url URL` sets the CAO address the relay's `cao-ops-mcp-server`
     uses (loopback `http` only). The default is `CAO_SHELF_CAO_URL` if set,
     else `http://127.0.0.1:9889`. Keep it equal to the page's `cao_url`
     setting: CAO Shelf refuses to start a relay when the two differ.
   - `--relay-model ID` sets the relay agent's model, for example
     `claude-haiku-4.5`, since the relay only forwards messages. The default
     is `auto`. The installer checks the id's shape, not your entitlement. See
     [a cheaper model for the relay](../examples/kiro-crew-cao-shelf/README.md#a-cheaper-model-for-the-relay).
     Instead of this option, you can also pin a model per App in Kiro Crew:
     Settings, Chat, Model, Apps, row CAO Shelf (`apps.cao-shelf.model`). That
     pin applies while the agent's model is `auto`.

4. Give the session-approval consent in the dashboard. On a first install the
   installer stops with exit code 7 and prints Kiro Crew's link to the App.
   Open it, or open the dashboard and go to Apps, Library, CAO Shelf (a
   disabled App is not in the sidebar yet), read what the App asks for, and
   enable it there. Kiro Crew accepts this consent only in the
   dashboard, never from a script. CAO Shelf needs it because Kiro Crew refuses
   an App's call that sets a chat's trust mode without it, and CAO Shelf sets
   each relay chat to `normal` trust mode. It approves no tool request.

5. Open the page: in the Kiro Crew dashboard, choose **CAO Shelf** in the
   sidebar (route `/apps/cao-shelf`). If the page says the App has no reachable
   backend, restart Kiro Crew the way you normally do.

Kiro Crew assigns the backend's loopback port when it starts the App, so you
configure no port.

## Configure

Open **Settings** at the foot of the CAO Shelf page. The settings are stored in
the App's own `data/config.json`:

| Setting | Default | What it does |
|---|---|---|
| `cao_url` | `http://127.0.0.1:9889` | Where CAO is. Only `http` on `127.0.0.1`, `localhost`, or `::1` is accepted. |
| `poll_seconds` | 5 | How often the page refreshes (3 to 60). |
| `warm_hours`, `stale_hours` | 1, 48 | The idle ages that mark a session warm and stale. |
| `persist_history` | on | Keeps a ledger of closed helpers: ids and times, no text. |

You cannot pass environment variables to an App, so the Settings panel is the
only way to point it at another CAO. The full table is in
[Configuration](../examples/kiro-crew-cao-shelf/README.md#configuration).

### Your ACP backend decides what a relay chat can reach

Kiro Crew runs chats on the backend named in its `agent.acp_backend` setting.
To see yours, run `kirocrew config get agent.acp_backend`.

- **`kas`.** The relay agent's tool list is a strict allowlist: the chat gets
  only its four CAO tools and Kiro Crew's `wait` tool, and they run without
  asking you. Your Claude Code settings do not apply. You need no allow rule.
- **`claude`.** The relay chat runs with your own Claude Code settings, so your
  plugins, user-level MCP servers, and other built-in tools also reach it. Kiro
  Crew refuses the shell and Claude Code's file-edit tools there. Each call of
  the five relay tools asks for your permission until you add the allow rule
  below.

On both backends, a plugin installed in Kiro Crew can add an MCP server to
every agent, the relay agent included.

### Add the Claude allow rule (claude backend only)

On the `claude` backend, add this rule once to your Claude Code user settings
(`~/.claude/settings.json`), merged into any `permissions.allow` list you
already have. It pre-approves the four CAO tools and Kiro Crew's `wait` tool.
CAO Shelf never edits that file.

```json
{
  "permissions": {
    "allow": [
      "mcp__cao-shelf_ops__send_session_message",
      "mcp__cao-shelf_ops__get_terminal_status",
      "mcp__cao-shelf_ops__read_session_output",
      "mcp__cao-shelf_ops__get_session_info",
      "mcp__kirocrew-core__wait"
    ]
  }
}
```

The rule is user-wide and matches by name. Read
[the one-time setup](../examples/kiro-crew-cao-shelf/README.md#one-time-setup-the-claude-allow-rule)
for what else it matches. On `kas` you skip this step.

## Remote control a session

1. On a session's card, press **Remote control**.
2. Read the stated risk, and the detail behind **What the chat can reach**,
   then press **Accept and remote control**. CAO Shelf creates a chat titled
   "Remote control: <session>" and opens it.
3. Type a message in the chat. The relay agent sends it to the conductor
   tagged `[REMOTE-CONTROL]` (a message that starts with `/` goes untagged),
   waits for the turn to end, and shows the reply between two marker lines.

Keep these points in mind:

- Idle does not mean done. A conductor can be idle while its helpers still
  work, so the relay agent reads each reply, checks back with pauses, and
  reports which helpers are still working.
- When the session waits for an approval, answer it in the **Answer** box on
  the session's card, then choose Check again in the chat.
- You can keep typing to the session directly. Your next message from the
  chat then carries the tag `[REMOTE-CONTROL catch-up]`, and the session
  opens its reply with a brief of what happened in between.
- **End relay** on the card stops the chat, never the CAO session.

To start a session and remote control it at once, tick **and remote control
it** in the Start dialog, then tick **I accept this risk**. The full behaviour
is in [Remote control](../examples/kiro-crew-cao-shelf/README.md#remote-control),
and the relay texts are in the
[remote control contract](../examples/kiro-crew-cao-shelf/api-contract.md#remote-control).

## Security notes

- Text from a CAO session reaches the relay chat's model, so an instruction
  hidden in it could make the model call a tool the chat has. An allowed tool
  runs without asking you. In a relay chat, refuse any permission request for
  a tool other than the four CAO tools and `wait`.
- On `claude`, keep Claude Code's `defaultMode` off `auto` and
  `bypassPermissions` while you use relays. If you enable the Claude Code
  sandbox, set `sandbox.autoAllowBashIfSandboxed: false` explicitly.
- The session-approval consent lets the App's backend act on any of your local
  Kiro Crew chats. CAO Shelf's code uses it only to set its own relay chats to
  `normal` trust mode; Kiro Crew does not enforce that limit.
- The backend talks only to CAO and its own Kiro Crew gateway on loopback,
  sends no telemetry, runs no shell, and stores no message text.

The details are in these sections of the App README:
[the trust step](../examples/kiro-crew-cao-shelf/README.md#the-trust-step-in-plain-words),
[what the chat can reach](../examples/kiro-crew-cao-shelf/README.md#what-the-chat-can-reach-read-this-first),
[what holds and what does not](../examples/kiro-crew-cao-shelf/README.md#what-holds-and-what-does-not),
[what it never does](../examples/kiro-crew-cao-shelf/README.md#what-it-never-does),
and [privacy](../examples/kiro-crew-cao-shelf/README.md#privacy).

## Update and uninstall

To update, pull the repository and run the same install command again. The
installer runs `kirocrew app update`, and Kiro Crew keeps the App's data. Kiro
Crew also keeps the model already set on the installed relay agent, so a new
`--relay-model` takes effect only after you uninstall and install again (then
enable CAO Shelf in the dashboard again). An update from version 0.1.0 asks
for the session-approval consent once more.

To uninstall:

```bash
examples/kiro-crew-cao-shelf/install.sh --uninstall                 # keeps data/
examples/kiro-crew-cao-shelf/install.sh --uninstall --purge-data    # deletes data/ too
```

Uninstalling removes the per-app trust grant. It never changes
`agent.apps_allow_third_party`; if you set it with `--trust-all`, turn it off
with `kirocrew config set agent.apps_allow_third_party false`.

## Troubleshoot

| You see | What to do |
|---|---|
| Enable is refused with `app_execution_denied`, or the installer exits 4 | Run the installer again with `--trust-app` or `--trust-all`. |
| The installer exits 7 | Enable CAO Shelf in the dashboard to give the session-approval consent (install step 4). |
| The installer exits 3 | The page's sources (`ui/src` or its build config) changed since the committed bundle was built, and Node 22.12 or later with npm is not on `PATH`. Install Node so the installer rebuilds it, or pass `--no-build` to install the committed bundle as it is. |
| The installer exits 6 | Put `cao-ops-mcp-server` on `PATH`, then run it again. Nothing was installed. |
| `app 'cao-shelf' has no reachable backend` | Restart Kiro Crew the way you normally do, and check the App's log in Kiro Crew. |
| "CAO is not reachable at ..." | Start `cao-server`, or fix `cao_url` in Settings. |
| Remote control says `relay_cao_mismatch` | Run the installer again with `--cao-url` set to the page's `cao_url`. |
| Remote control says `gateway_refused (session_approval_not_granted)` | Enable CAO Shelf in the dashboard. |
| A relayed card says a tool request is waiting | Deny it, unless you are on `claude`, have not added the allow rule yet, and the chat shows one of the five relay tools. |

The full table is in
[Troubleshooting](../examples/kiro-crew-cao-shelf/README.md#troubleshooting).

## See also

- [CAO Shelf README](../examples/kiro-crew-cao-shelf/README.md): the full
  guide, verified behaviour, and limits
- [CAO Shelf proposal](../examples/kiro-crew-cao-shelf/PROPOSAL.md): why the
  App exists and how it is designed
- [CAO Shelf backend contract](../examples/kiro-crew-cao-shelf/api-contract.md):
  the routes and the remote control texts
- [Control planes](control-planes.md): choose between the Web UI, the CLI, and
  `cao-ops-mcp`
- [Web UI](web-ui.md) and [MCP Apps](mcp-apps.md): CAO's other visual surfaces
