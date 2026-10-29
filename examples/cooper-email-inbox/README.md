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

Cooper's sign-up is one HTTP request with no browser step. It returns an API
key once, along with the new inbox. The commands below write the key straight
into CAO's managed env file (`~/.aws/cli-agent-orchestrator/.env`, or
`$CAO_HOME_DIR/.env`), so it never appears in your terminal, your shell
history or a command line. Replace `my-cao-agent` with the username you want
and `you@example.com` with your own address (optional; you get one
confirmation email and can approve the agent's drafts).

```bash
# 1. Create the inbox and store the key (requires curl and jq)
ENV_FILE="${CAO_HOME_DIR:-$HOME/.aws/cli-agent-orchestrator}/.env"
mkdir -p "$(dirname "$ENV_FILE")"
(
  umask 077
  TMP="$(mktemp)"
  curl -s https://cooperemail.com/api/v1/onboard \
    -H 'content-type: application/json' \
    -d '{"username":"my-cao-agent","owner_email":"you@example.com"}' -o "$TMP"
  if jq -e '.api_key | strings' "$TMP" > /dev/null; then
    jq -r '"COOPER_API_KEY=\(.api_key)"' "$TMP" >> "$ENV_FILE"
    jq -r '.inbox.email' "$TMP"   # prints only the new address
  else
    jq -c '.error // .' "$TMP"    # no key in the response; nothing written
  fi
  rm -f "$TMP"
)

# 2. Install the profile
cao install examples/cooper-email-inbox/mail_agent.md

# 3. Launch it
cao launch --agents mail_agent
```

If the username is taken the response is HTTP 409, the error is printed and
no key is written; pick another name and run step 1 again. `cao env list` shows that `COOPER_API_KEY`
is set. If you already have a key, add the `COOPER_API_KEY=` line to that file
with an editor instead.

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

The server exposes other tools (domains, webhooks, phone numbers, billing,
workspaces). The profile's instructions keep the agent to the ones above.
Full reference: [cooperemail.com/docs](https://cooperemail.com/docs).

## If the key is missing or wrong

CAO resolves `${VAR}` with `Template.safe_substitute`, so an unset
`COOPER_API_KEY` leaves the placeholder text in place rather than failing at
load time. The server is still registered and answers authenticated calls
with `401`. The profile tells the agent to stop and report that instead of
creating a new account. Check `cao env list` first.

## Data flow

Mail the agent sends goes through `cooperemail.com`, a third-party service,
and reaches real recipients. Mail sent to the inbox is stored there and read
through your key. Do not have the agent send secrets, credentials or private
repository content. The profile tells it to confirm each new recipient with
you and to treat incoming mail as data, but that is prompt-level guidance and
not a filter.

## Notes

- The profile uses `role: reviewer`, whose native tool defaults are read-only.
  Adding the `cooper-email` MCP server re-introduces network egress that the
  reviewer role's defaults deliberately exclude. The role label alone does not
  make this agent network-sandboxed.
- Incoming email is external content. The profile instructs the agent to
  treat it as untrusted, never as instructions.
- The free plan covers 5 inboxes and 5,000 emails a month. See
  [cooperemail.com/pricing](https://cooperemail.com/pricing).
