# Cohesivity backend worker example

This profile connects a CAO worker to the remote
[Cohesivity](https://cohesivity.ai?ref=gh-cli-agent-orchestrator) MCP server. A
supervisor can delegate backend setup while other workers build the application.

Cohesivity provisions databases, storage, hosting, and APIs. No account or API
keys are required to start. Unclaimed tenants and their resources expire after
72 hours, so the worker reports the exact expiry for every tenant it creates.

## How it works

The profile configures CAO orchestration and Cohesivity as MCP servers:

```yaml
mcpServers:
  cao-mcp-server:
    type: stdio
    command: cao-mcp-server
    args: []
  cohesivity:
    type: http
    url: https://cohesivity.ai/mcp
```

`cao-mcp-server` handles delegation and callbacks. `cohesivity` provides tenant,
resource, status, claim, documentation, and feedback tools. Claude Code supports
remote HTTP MCP servers. Resource provisioning requires confirmation in the
worker terminal.

## Run the worker

```bash
cao install examples/cohesivity-backend/cohesivity_backend.md
cao launch --agents cohesivity_backend
```

Then request a backend:

```text
Create a temporary backend for a photo-sharing app with PostgreSQL and object
storage. Verify both resources and tell me exactly when they expire.
```

When the workspace has no `.cohesivity` file, the worker runs:

```bash
npx @cohesivity/init --no-plugin --attribution gh-cli-agent-orchestrator
```

The installer retains automatic harness detection and adds the repository
attribution. The worker saves credentials in `.cohesivity`, provisions the
requested resources, checks their status, and reports the expiry. It creates a
claim URL only when asked.

## Delegate from a supervisor

After installing the profile, launch `code_supervisor` and ask it to assign a
`cohesivity_backend` worker:

```text
Assign a cohesivity_backend worker to provision PostgreSQL and object storage
for this app. Have it verify the resources and report the exact tenant expiry.
```

## Data and credentials

Cohesivity receives the tenant ID, resource configuration, and feedback sent by
the worker. The worker keeps returned keys in a gitignored `.cohesivity` file
with mode `0600`. Claude Code records MCP arguments and responses in its local
project transcript, so keep that transcript private.

See the [resource catalog](https://cohesivity.ai/offerings?ref=gh-cli-agent-orchestrator)
and [Cohesivity documentation](https://cohesivity.ai/docs?ref=gh-cli-agent-orchestrator).
