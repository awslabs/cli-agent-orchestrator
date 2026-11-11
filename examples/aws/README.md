# AWS Cloud-Ops Agent Examples

Ready-to-use agent profiles for common AWS operational tasks. Each agent lives
in its own folder with a profile (`.md`) and a configuration reference (`config.json`).

## Agents

| Folder | Description |
|--------|-------------|
| [stepfunction/](stepfunction/) | Trigger and monitor AWS Step Functions executions |
| [cloudwatch-logs/](cloudwatch-logs/) | Search CloudWatch Logs for execution traces and error patterns |
| [dynamodb-query/](dynamodb-query/) | Query DynamoDB tables by partition key |
| [dynamodb-delete/](dynamodb-delete/) | Delete all items matching a partition key |
| [sqs-monitor/](sqs-monitor/) | Poll an SQS queue until all messages are consumed |
| [sqs-send/](sqs-send/) | Send a message to an SQS queue |
| [sqs-dlq-check/](sqs-dlq-check/) | Inspect a Dead Letter Queue for failed messages |

## Prerequisites

- [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
- A named AWS profile (`aws configure --profile my-profile`)
- `jq` installed (used for JSON parsing)
- `uuidgen` installed (used by stepfunction and sqs-send for unique IDs)
- IAM permissions scoped to the specific service (see each agent's profile)

## How to Use

### 1. Check config.json for the values you need

Each folder has a `config.json` that documents every configurable value with
example placeholders:

```json
{
  "profile": "my-aws-profile",
  "region": "us-east-1",
  "queue_url": "https://sqs.us-east-1.amazonaws.com/123456789012/MyQueue"
}
```

### 2. Install with your values

All values are **required** at install time. Pass them via `--env` flags. The
`${VAR}` placeholders in the `.md` profile are resolved during install:

```bash
cao install examples/aws/sqs-monitor/sqs-monitor-agent.md \
  --env AWS_PROFILE=my-profile \
  --env AWS_REGION=us-west-2 \
  --env QUEUE_URL=https://sqs.us-west-2.amazonaws.com/111111111111/my-queue \
  --env POLL_INTERVAL_SECONDS=5 \
  --env TIMEOUT_SECONDS=60
```

If any `--env` value is omitted, the `${VAR}` placeholder passes through
unresolved and the agent's empty-var validation will exit with an error at
runtime.

### 3. Launch the agent

```bash
cao launch --agents sqs-monitor-agent --provider claude_code
```

## Use in a Workflow

Each profile declares the values it needs as `params` in its frontmatter, so a
YAML workflow step that uses it can set them. The param names are the
`config.json` keys. The declarations come from the matching template schema in
`src/cli_agent_orchestrator/templates/aws/<name>/schema.json`: the required
list, the descriptions, the enums and the defaults.

A copy installed before these examples declared params has no `params` block.
Reinstall it with `cao install examples/aws/<name>/<name>-agent.md --provider <provider>`.

```yaml
steps:
  - id: read-orders
    provider: claude_code
    agent: dynamodb-query-agent
    params:
      profile: "my-aws-profile"
      region: "us-east-1"
      table_name: "orders"
      partition_key_name: "pk"
      partition_key_value: "customer-42"
      partition_key_type: "S"
      limit: "1"
    prompt: "Read the newest order for this customer."
```

The engine adds the values to the step prompt under "Parameters for this
step:", and the profile's instructions tell the agent to use them in place of
the `${...}` placeholders. Outside a workflow, the placeholders work as before.
Validation and run start check the values against the declarations: a required
param must be set, `limit` must be a whole number, and `partition_key_type`
must be `S`, `N` or `B`. See
[Params a profile declares](../../docs/workflows.md#params-a-profile-declares).

Param values are journaled in plaintext. Pass the name of an AWS profile, never
a credential.

## Profile Discovery

Each profile declares `tags` and `capabilities` in its frontmatter. These
fields are indexed by `cao profile find` (and the `find_profiles` MCP tool),
so installed agents can be discovered by what they do:

```bash
cao profile find "dead letter queue"
cao profile find "query dynamodb"
```

Only `name`, `description`, `tags`, and `capabilities` are indexed; the
profile body is never indexed or returned in search results. When you copy
these examples for your own agents, keep tags as short keywords and
capabilities as short verb phrases.

## Configuration Reference

The `config.json` in each folder is **not** read at runtime. It exists as a
reference for what `--env` values to pass during install. The mapping from
config keys to env var names:

| config.json key | `--env` variable |
|-----------------|------------------|
| `profile` | `AWS_PROFILE` |
| `region` | `AWS_REGION` |
| `queue_url` | `QUEUE_URL` |
| `table_name` | `TABLE_NAME` |
| `state_machine_arn` | `STATE_MACHINE_ARN` |
| `dlq_url` | `DLQ_URL` |
| `log_group` | `LOG_GROUP` |

Service-specific keys (e.g., `poll_interval_seconds`, `max_messages`) map to
their uppercased equivalents (`POLL_INTERVAL_SECONDS`, `MAX_MESSAGES`).

## Security Notes

- All agents use explicit `--profile` flags, never default credentials
- Each agent validates that required variables are non-empty before executing
- The `dynamodb-delete` agent enforces a `MAX_DELETE` safety cap and requires
  explicit `CONFIRM=yes` to proceed with deletions
- These agents process inputs from trusted supervisors only; do not expose
  to untrusted message sources without additional input sanitization
- Never store real credentials in agent profile files

## Provider Compatibility

`allowedTools` restrictions are enforced by `claude_code` and `codex` providers.
The `kiro_cli` provider does not currently enforce tool restrictions at the CAO
level. Use `--provider claude_code` for enforced sandboxing.

## Multi-Agent Orchestration

These agents are designed to work standalone or as workers in a multi-agent
system. Each includes a `cao-mcp-server` block so supervisors can delegate
via `handoff` or `send_message`. See the [assign example](../assign/) for
orchestration patterns.
