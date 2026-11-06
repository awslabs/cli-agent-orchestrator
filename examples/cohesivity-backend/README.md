# Cohesivity backend worker example

This profile connects a CAO worker to the remote
[Cohesivity](https://cohesivity.ai) MCP server. A supervisor can delegate
backend setup while other workers build the application.

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

`cao-mcp-server` handles worker callbacks to the supervisor. `cohesivity`
provides tenant, resource, status, claim, documentation, and feedback tools.
Claude Code supports remote HTTP MCP servers.

The profile sets `allowedTools` to the reviewer defaults plus file writes. The
worker can read files and write `.cohesivity` and `.gitignore`. It has no
shell and no web fetch, so setup does not run or download any package.

## Permissions

The profile sets no `permissionMode`, so CAO launches Claude Code with
`--dangerously-skip-permissions` (see [Claude Code](../../docs/claude-code.md)).
Tenant creation, provisioning, and file writes can run without a prompt once
you ask for a backend. To approve each call yourself, add `permissionMode: default`
to the frontmatter and answer the prompts in the worker terminal. Delegated
workers wait at those prompts until you do.

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

## What setup does

When the workspace has no `.cohesivity` file, the worker:

1. Calls the `create_tenant` MCP tool once.
2. Writes the returned credentials to `.cohesivity` in the working directory.
3. Adds `.cohesivity` to `.gitignore`.

It changes no other files and installs nothing. It then provisions the
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

Cohesivity receives the MCP client name that Claude Code sends when it
connects, the tool calls the worker makes (tenant creation, resource
configuration, status checks, claims), and any feedback. The profile adds no
tracking ID.

`.cohesivity` holds the tenant's keys. Claude Code's file write uses your
default file mode, so run `chmod 600 .cohesivity` on a shared machine. Claude
Code also records MCP arguments and responses in its local project
transcript, so keep that transcript private.

This example is maintained by Cohesivity. See the
[Cohesivity documentation](https://cohesivity.ai/docs).
