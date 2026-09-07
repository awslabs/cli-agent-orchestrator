# You.com Web Research Example

A ready-to-run research agent profile that wires the [You.com MCP server](https://you.com/docs)
into a CAO terminal through the profile's `mcpServers` remote-URL mechanism —
no code, no local dependencies, one config entry.

The agent answers questions that depend on current web information using the
`you-search` (web search) and `you-contents` (URL content extraction) MCP
tools, and cites its sources.

## How It Works

CAO agent profiles support remote MCP servers directly:

```yaml
mcpServers:
  youcom:
    type: http
    url: https://api.you.com/mcp?profile=free
```

The entry above uses You.com's keyless endpoint — no API key or account
needed. The URL-server shape passes through to providers unchanged (see
[Agent profiles](../../docs/agent-profile.md)); providers that support remote
HTTP transports (for example Claude Code, Grok, and MiniMax Code) connect to it
at launch. The profile pins `provider: claude_code` for this reason — the
default `kiro_cli` provider's support for remote `type: http` MCP servers is
undocumented, so launching on defaults could silently produce an agent with no
search tools. Swap the `provider` key (or pass `--provider` at launch) to run
it on another HTTP-capable provider.

The profile also keeps `cao-mcp-server` configured so the agent can still be
targeted by supervisors via `handoff`/`assign` — it works both standalone and
inside a fleet.

## Setup

```bash
# 1. Install the profile
cao install examples/youcom-search/youcom_researcher.md

# 2. Launch it
cao launch youcom_researcher
```

Then ask anything that needs current information:

```text
Find the current recommended way to configure uv workspaces and cite sources.
What changed in the latest Python release notes?
```

## Authenticated Endpoint (optional)

The keyless `profile=free` endpoint provides basic search. For the full
You.com MCP toolset, use the authenticated endpoint:

```yaml
mcpServers:
  youcom:
    type: http
    url: https://api.you.com/mcp
```

and configure `YDC_API_KEY` bearer auth (get a key at
[you.com/platform/api-keys](https://you.com/platform/api-keys)). How the
bearer header is attached depends on the provider's MCP client; check your
provider's remote-MCP auth options. Keep the key in your provider's MCP
client config or environment — do not paste it into the profile markdown,
which is meant to be shared and committed. Alternatively, run the skill-based
setup from [youdotcom-oss/agent-skills](https://github.com/youdotcom-oss/agent-skills)
(`npx skills add youdotcom-oss/agent-skills`), which routes agents to the
lightest You.com surface for the host. That command executes third-party
code from the registry, so verify the publisher matches `youdotcom-oss`
before running it.

## Data flow

Every search query and every `you-contents` URL fetch is sent to
`api.you.com` — a third-party service. The keyless `profile=free` endpoint
has no account boundary or audit trail. Do not include sensitive, secret, or
repo-local content in research queries; assume anything the agent sends in a
query leaves the machine.

## Notes

- The profile uses `role: reviewer`, whose native tool defaults are
  read-only. Be aware that adding the `youcom` MCP server re-introduces
  network egress (search + arbitrary URL fetch) that the reviewer role's
  defaults deliberately exclude — the role label alone does not make this
  agent network-sandboxed. The profile's security constraints are prompt-level
  guidance, not an enforced boundary. Widen `allowedTools` in the profile if
  you want the agent to also edit files based on its findings.
- Search results and fetched page content are external data; the profile
  instructs the agent to treat them as untrusted evidence, not instructions.
