# Serply Scholar and News Research Example

A research agent profile that wires the [Serply](https://serply.io) MCP server
into a CAO terminal through the profile's `mcpServers` remote-URL mechanism.
No code, no local dependencies, one config entry.

Where a general web search answers "what does the internet say", this profile
targets the two surfaces that carry their own evidence:

- **Google Scholar** results with a **citation count** per paper, so the agent
  can weigh prior work instead of guessing at its standing.
- **Google News** results with the **publisher and publication date**, so a
  claim about current state can be dated rather than asserted.

General Google web search and page scraping are there too, but the verticals
are the reason to install this one. Serply's tool list and response fields are
documented at [serply.io/docs](https://serply.io/docs).

## How It Works

CAO agent profiles support remote MCP servers directly, and unlike a keyless
endpoint this one authenticates with a header:

```yaml
mcpServers:
  serply:
    type: http
    url: https://api.serply.io/mcp
    headers:
      X-Api-Key: ${SERPLY_API_KEY}
```

The URL-server shape passes through to providers unchanged (see
[Agent profiles](../../docs/agent-profile.md)); providers that support remote
HTTP transports connect to it at launch. The profile pins
`provider: claude_code` because the default `kiro_cli` provider's support for
remote `type: http` MCP servers is undocumented, so launching on defaults could
silently produce an agent with no search tools. `grok_cli` and `minimax_code`
read the `headers` field explicitly and work too; swap the `provider` key or
pass `--provider` at launch.

**The key never goes in the profile.** `${SERPLY_API_KEY}` is resolved by CAO's
own managed environment (`cao env set`, or `--env` at install time) when the
profile is loaded, so the committed markdown carries a placeholder and not a
secret. That matters because a profile is meant to be shared and committed.

The profile also keeps `cao-mcp-server` configured so the agent can still be
targeted by supervisors via `handoff`/`assign`. It works standalone and inside
a fleet.

## Setup

Get a key at [serply.io](https://serply.io), then:

```bash
# 1. Install the profile, writing the key to the managed env file
cao install examples/serply-research/serply_researcher.md \
  --env SERPLY_API_KEY=your-serply-api-key

# 2. Launch it
cao launch --agents serply_researcher
```

To rotate the key later, use `cao env set SERPLY_API_KEY new-value`. There is
no need to reinstall or edit the profile.

Then ask anything that wants evidence behind it:

```text
What are the most cited papers on retrieval augmented generation, and what do they claim?
What has been published about AWS open source releases in the last month?
```

## Tools

| Tool | What it returns |
| --- | --- |
| `google_scholar_search` | Academic results with authors, venue, year, citation count |
| `google_news_search` | News articles with publisher and publication date |
| `google_search` | Google web results; `site:`, `filetype:` and `intitle:` operators pass through in the query |
| `scrape_url` | Readable content for one URL |
| `bing_search`, `google_maps_search`, `google_jobs_search`, `google_video_search`, `amazon_product_search` | Other verticals |
| `reddit_subreddit_posts`, `reddit_subreddit_about`, `reddit_user_posts`, `reddit_post_comments`, `reddit_post` | Reddit reads |

Fourteen tools in total. The profile's instructions steer the agent to the
first four; the rest are available if a question needs them.

## If the key is missing or wrong

CAO resolves `${VAR}` with `Template.safe_substitute`, so an unset
`SERPLY_API_KEY` leaves the placeholder text in place rather than failing at
load time. The server is still registered, and Serply answers its calls with
`401`. If the agent reports authentication failures from every search tool,
check `cao env list` first.

## Data flow

Every search query and every URL passed to `scrape_url` is sent to
`api.serply.io`, a third-party service, authenticated with your key and
therefore attributable to your account. Do not include sensitive, secret or
repo-local content in research queries; assume anything the agent sends in a
query leaves the machine.

## Notes

- The profile uses `role: reviewer`, whose native tool defaults are read-only.
  Adding the `serply` MCP server re-introduces network egress (search queries
  and `scrape_url` fetches) that the reviewer role's defaults deliberately
  exclude. The role label alone does not make this agent network-sandboxed, and
  the profile's security constraints are prompt-level guidance rather than an
  enforced boundary. Widen `allowedTools` if you want the agent to also edit
  files based on its findings.
- Search results and scraped page content are external data. The profile
  instructs the agent to treat them as untrusted evidence, not as instructions.
- Citation counts measure attention, not correctness. The profile tells the
  agent to report the number rather than to editorialize from it.
