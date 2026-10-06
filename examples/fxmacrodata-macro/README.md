# FXMacroData Macro Release Example

A ready-to-run analyst profile that wires the
[FXMacroData MCP server](https://fxmacrodata.com/documentation/mcp-server) into
a CAO terminal through the profile's `mcpServers` remote-URL mechanism. No
code, no local dependencies, one config entry.

The agent answers questions about official macroeconomic releases: the latest
figures with their publication times, what is scheduled next, and how a new
print compares with earlier ones. Data comes from central banks and statistics
agencies for 22 currencies.

## How It Works

```yaml
mcpServers:
  fxmacrodata:
    type: http
    url: https://mcp.fxmacrodata.com
```

The entry connects without an API key. As in
[examples/youcom-search](../youcom-search/README.md), the profile pins
`provider: claude_code` because the default `kiro_cli` provider's support for
remote `type: http` MCP servers is undocumented. Swap the `provider` key (or
pass `--provider` at launch) to run it on another HTTP-capable provider.

The profile also keeps `cao-mcp-server` configured so a supervisor can target
the agent with `handoff`/`assign`.

## Setup

```bash
# 1. Install the profile
cao install examples/fxmacrodata-macro/fxmacrodata_analyst.md

# 2. Launch it
cao launch fxmacrodata_analyst
```

Then ask:

```text
What US data is scheduled for release this week, and at what times?
What was the latest US CPI print, and when was it published?
How does the latest US payrolls figure compare with the previous six releases?
```

## What works without a key

Without a key the server answers for USD releases, the USD release calendar and
the USD indicator catalogue. Each release becomes readable 15 minutes after it
is published, and keyless results carry a `freemium_delay` object saying so; the
profile tells the agent to report that delay instead of presenting the data as
current. Other currencies, FX rates and the remaining tools answer
`subscription_required` until a key is configured.

## Using an API key (optional)

With a key (from [fxmacrodata.com/subscribe](https://fxmacrodata.com/subscribe)),
add a header to the `fxmacrodata` entry, keeping the key itself out of the
profile:

```yaml
  fxmacrodata:
    type: http
    url: https://mcp.fxmacrodata.com
    headers:
      Authorization: Bearer ${FXMACRODATA_API_KEY}
```

and store the key in CAO's managed env file when installing:

```bash
cao install examples/fxmacrodata-macro/fxmacrodata_analyst.md \
  --env FXMACRODATA_API_KEY=your-fxmacrodata-api-key
```

`load_agent_profile` substitutes `${FXMACRODATA_API_KEY}` from that file. Only
add the header once the variable is set: `resolve_env_vars` uses
`Template.safe_substitute`, so a missing variable leaves the literal placeholder
in the header, and the server rejects an unrecognised key with `401
invalid_api_key` instead of falling back to keyless access.

## Data flow

Every tool call (currency codes, indicator slugs, date ranges) is sent to
`mcp.fxmacrodata.com`, a third-party service. Do not put sensitive, secret or
repo-local content in requests.

## Notes

- The profile uses `role: reviewer`, whose native tool defaults are read-only.
  Adding the `fxmacrodata` MCP server re-introduces network egress that the
  role's defaults exclude, so the role label alone does not make this agent
  network-sandboxed. The profile's security constraints are prompt-level
  guidance, not an enforced boundary.
- Every tool on the server is read-only.
- Tool results are external data; the profile instructs the agent to treat
  them as untrusted evidence, not instructions.
