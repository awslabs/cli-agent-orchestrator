# deed.page Publisher Example

An agent profile that wires the [deed.page](https://deed.page) MCP server into
a CAO terminal through the profile's `mcpServers` remote-URL mechanism, so a
supervisor or a user can turn agent output into a public web page and get a
link back. No code, no local dependencies, no API key, one config entry.

The other remote-server examples ([serply-research](../serply-research/README.md),
[fxmacrodata-macro](../fxmacrodata-macro/README.md)) bring data into a
session. This one goes the other way: it takes something an agent produced (a
review, a research summary, a test report) and returns a URL you can paste
into a ticket or a chat.

## How It Works

CAO agent profiles support remote MCP servers directly. deed.page's endpoint
needs no key, so the entry has no `headers`:

```yaml
mcpServers:
  deed-page:
    type: http
    url: https://deed.page/mcp
```

The URL-server shape passes through to providers unchanged (see
[Agent profiles](../../docs/agent-profile.md)). The profile pins
`provider: claude_code` because the default `kiro_cli` provider's support for
remote `type: http` MCP servers is undocumented. `grok_cli` and `minimax_code`
work too; swap the `provider` key or pass `--provider` at launch.

The profile keeps `cao-mcp-server` configured, so a supervisor can `handoff`
finished work to it and get the URL back, and it works standalone too.

## Setup

```bash
# 1. Install the profile
cao install examples/deed-page-publisher/page_publisher.md

# 2. Launch it
cao launch --agents page_publisher
```

Then hand it something to publish:

```text
Publish docs/release-notes.md as a page.
Turn this review summary into a page with a table of the findings: ...
```

From a supervisor, delegate the last step of a flow with `handoff` to
`page_publisher` and include the content in the message.

## Tools

| Tool | What it does |
| --- | --- |
| `publish_site` | Publishes a `files` map and returns `url`, `slug`, `version`, and an owner `token` on the first publish; `ttl_seconds` sets an expiry |
| `update_site` | Replaces the files of a page published earlier, using its `slug` and `token` |
| `get_site` | Reports whether a slug is live (no token needed) |
| `delete_site` | Deletes a page published earlier, with its `slug` and `token`; earlier versions go with it |

The server exposes other tools (custom domains, webhooks, analytics, token
rotation, billing). The profile's instructions keep the agent to the four
above. Full reference: [deed.page/docs](https://deed.page/docs).

## Expiry and the owner token

By default the profile tells the agent to publish with `ttl_seconds: 604800`,
so pages expire after seven days unless you ask for a permanent one.

The first publish returns an owner `token`, which is the only way to update or
delete that page. It arrives in the tool result, so it is in the agent's
context and the provider's transcript. The profile tells the agent not to
pass it on or repeat it unless you ask for it directly. To delete a page
before it expires, ask the agent to delete it in the same session (it calls
`delete_site`). Outside the session, ask for the token and run
`curl -X DELETE https://deed.page/v1/sites/<slug> -H "Authorization: Bearer <token>"`.

## Versions

Each publish or update stays public as a version at
`https://v{n}--<slug>.deed.page/` until retention drops it (the free plan keeps
the last 10). An update does not remove earlier content. To remove it, delete
the site: deleting a site removes all of its versions. The profile tells the
agent to warn you and offer a delete when a revision removes content that must
not stay public.

## Data flow

Everything the agent publishes is sent to `deed.page`, a third-party service,
and served at a public URL that anyone with the link can open. Do not publish
secrets, credentials or private repository content. The profile tells the
agent to stop and ask when the content looks private, but that is prompt-level
guidance and not a filter.

The profile tells the agent to escape the content and to add a
`script-src 'none'` Content-Security-Policy meta tag, so raw HTML or script in
the content is shown as text and does not run for readers of the page.

Pages on the free plan carry a small "Published with deed.page" badge. The
server's instructions may mention paid plans or other offers; the profile tells
the agent not to pass them on unless you ask.

## Notes

- The profile uses `role: reviewer`, whose native tool defaults are read-only
  (it can read files it is asked to publish, but cannot edit them or run
  commands). Adding the `deed-page` MCP server re-introduces network egress
  that the reviewer role's defaults deliberately exclude. The role label alone
  does not make this agent network-sandboxed.
- Content to publish is treated as data, not instructions.
