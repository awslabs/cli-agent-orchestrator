# Cooper Email Inbox Example

An agent profile that wires the [Cooper Email](https://cooperemail.com) MCP
server into a CAO terminal through the profile's `mcpServers` remote-URL
mechanism, so an agent in a session has its own email address. No code, no
local dependencies, one config entry.

The other remote-server examples bring data in
([serply-research](../serply-research/README.md),
[fxmacrodata-macro](../fxmacrodata-macro/README.md)) or publish output
([deed-page-publisher](../deed-page-publisher/README.md)). This one gives a
fleet a mailbox: a worker that signs up for a service can wait for the
verification code, a supervisor can hand off "check whether the vendor
replied", and a finished report can go to a person by email once the user
approves the recipient.

## How It Works

The endpoint authenticates with a bearer header, in the same shape as the
Serply example:

```yaml
mcpServers:
  cooper-email:
    type: http
    url: https://cooperemail.com/mcp
    headers:
      Authorization: Bearer ${COOPER_API_KEY}
```

The URL-server shape passes through to providers unchanged (see
[Agent profiles](../../docs/agent-profile.md)). The profile pins
`provider: claude_code` because the default `kiro_cli` provider's support for
remote `type: http` MCP servers is undocumented. `grok_cli` and `minimax_code`
read the `headers` field and work too; swap the `provider` key or pass
`--provider` at launch.

**The key never goes in the profile.** CAO resolves `${COOPER_API_KEY}` from
its managed environment file when the profile is loaded, so the committed
markdown carries a placeholder and not a secret.

The profile keeps `cao-mcp-server` configured, so a supervisor can `handoff`
an email task to it, and it works standalone too.

## Setup

Cooper's sign-up is one HTTP request with no browser step. It returns the
account's API key once, along with the new inbox. That key has full access to
the account, so the agent never gets it. The block below keeps it in a header
file for you and uses it one time to mint a second key that is scoped to this
inbox and can only read mail and send. Only the scoped key goes into CAO's
managed env file (`~/.aws/cli-agent-orchestrator/.env`, or
`$CAO_HOME_DIR/.env`). Neither key appears in your terminal, your shell
history or a command line. Set `USERNAME` to the name you want and `OWNER` to
your own address (optional; you get one confirmation email, and it lets you
turn on send approval below).

```bash
# 1. Create the inbox and store an inbox-scoped key (requires curl and jq)
USERNAME=my-cao-agent
OWNER=you@example.com
ENV_FILE="${CAO_HOME_DIR:-$HOME/.aws/cli-agent-orchestrator}/.env"
ACCOUNT_HDR="${XDG_CONFIG_HOME:-$HOME/.config}/cooper-email/$USERNAME.account.hdr"
mkdir -p "$(dirname "$ENV_FILE")" "$(dirname "$ACCOUNT_HDR")"
(
  umask 077
  ON="$(mktemp)"; KEY="$(mktemp)"
  jq -n --arg u "$USERNAME" --arg o "$OWNER" \
    '{username: $u} + (if $o == "" then {} else {owner_email: $o} end)' |
    curl -sS https://cooperemail.com/api/v1/onboard \
      -H 'content-type: application/json' -d @- -o "$ON"
  if jq -e '.api_key | strings' "$ON" > /dev/null; then
    # Full-access account key: a header file for you, never given to the agent
    jq -r '"Authorization: Bearer \(.api_key)"' "$ON" > "$ACCOUNT_HDR"
    # Mint a key that can only list this inbox, read its mail and send from it
    curl -sS "https://cooperemail.com/api/v1/inboxes/$(jq -r '.inbox.id' "$ON")/keys" \
      -H @"$ACCOUNT_HDR" -H 'content-type: application/json' \
      -d '{"name":"cao-mail-agent","permissions":["inbox_read","message_read","message_send"]}' \
      -o "$KEY"
    if jq -e '.key | strings' "$KEY" > /dev/null; then
      # Leading newline: safe even if the file's last line has no newline
      jq -r '"\nCOOPER_API_KEY=\(.key)"' "$KEY" >> "$ENV_FILE"
      jq -r '.inbox.email' "$ON"   # prints only the new address
    else
      jq -c '.error // .' "$KEY"   # no scoped key; nothing written to .env
    fi
  else
    jq -c '.error // .' "$ON"      # no key in the response; nothing written
  fi
  rm -f "$ON" "$KEY"
)

# 2. Install the profile
cao install examples/cooper-email-inbox/mail_agent.md

# 3. Launch it
cao launch --agents mail_agent
```

If the username is taken the response is HTTP 409, the error is printed and
no key is written; pick another name and run step 1 again.
`cao env list | cut -d= -f1` shows that `COOPER_API_KEY` is set without
printing its value. If you already have a Cooper account, mint an
inbox-scoped key with `POST /api/v1/inboxes/{id}/keys` and the permissions
above, and add the `COOPER_API_KEY=` line to that file with an editor. Do not
put a full-access key there.

The account key in `$ACCOUNT_HDR` (mode 600) is what you use for admin work:
minting or revoking keys (`DELETE /api/v1/keys/{id}`), deleting the inbox, or
the approval step below. You can move it to a password manager and delete the
file. The profile tells the agent not to read it, but its `reviewer` role has
`fs_read`, so a file outside the agent's reach is the safer choice.

### Optional: hold every send for your approval

After you click the confirmation link that Cooper emails to `OWNER`, turn on
approval mode. Cooper then holds each message the agent sends until you
approve it, and holds suspicious inbound mail for review instead of
delivering it:

```bash
curl -sS -X PATCH "https://cooperemail.com/api/v1/inboxes/$USERNAME@cooperemail.com" \
  -H @"$ACCOUNT_HDR" -H 'content-type: application/json' \
  -d '{"send_mode":"approval","screening":"hold"}' -o /dev/null -w '%{http_code}\n'
```

`200` means it is on. `409` means the owner address is not confirmed yet.
The scoped key has no `inbox_update` permission, so the agent cannot turn
approval mode off.

Then give it something to do:

```text
I just signed up for the staging dashboard with this agent's address. Wait for the code.
Did anyone reply to the vendor thread? Summarize it.
Email the release notes in docs/release-notes.md to qa@example.com.
```

The last one stops and asks you to confirm the recipient and the message
before anything is sent.

## Tools

| Tool | What it does |
| --- | --- |
| `cooper_whoami` | Confirms the key and names the account |
| `cooper_list_inboxes` | Lists inboxes with ids and addresses |
| `cooper_list_messages`, `cooper_get_message` | Lists mail and reads one message (`extracted_text` drops quoted history) |
| `cooper_wait_for_code` | Waits for a verification or login code and returns just the code |
| `cooper_wait_for_message` | Waits for the next message |
| `cooper_send_message`, `cooper_reply_message` | Sends a new message or replies in a thread |

The server lists other tools (rules, webhooks, owners, domains, workspaces,
keys, billing). CAO cannot hide single MCP tools, so the limit is the key: the
scoped key from step 1 gets `permission_denied` from the server for rules,
webhooks, owners, inbox changes, deleting the inbox and minting keys, and an
inbox-scoped key cannot call workspace routes at all. It can still send,
reply and forward from this inbox, so the "confirm each new recipient" rule
is prompt-level unless you turn on approval mode above.
Full reference: [cooperemail.com/docs](https://cooperemail.com/docs).

## If the key is missing or wrong

CAO resolves `${VAR}` with `Template.safe_substitute`, so an unset
`COOPER_API_KEY` leaves the placeholder text in place rather than failing at
load time. The server is still registered and answers authenticated calls
with `401`. The profile tells the agent to stop and report that instead of
creating a new account. Check `cao env list | cut -d= -f1` first.

## Data flow

Mail the agent sends goes through `cooperemail.com`, a third-party service,
and reaches real recipients. Mail sent to the inbox is stored there and read
through your key. Do not have the agent send secrets, credentials or private
repository content. The profile tells it to confirm each new recipient with
you and to treat incoming mail as data. That is prompt-level guidance; the
enforced limits are the scoped key and, if you turn it on, approval mode.

## Notes

- The profile uses `role: reviewer`, whose native tool defaults are read-only.
  Adding the `cooper-email` MCP server re-introduces network egress that the
  reviewer role's defaults deliberately exclude. The role label alone does not
  make this agent network-sandboxed.
- Incoming email is external content. The profile instructs the agent to
  treat it as untrusted, never as instructions.
- Plans and limits: [cooperemail.com/pricing](https://cooperemail.com/pricing).
