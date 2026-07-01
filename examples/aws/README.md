# AWS Cloud-Ops Agent Examples

Ready-to-use agent profiles for common AWS operational tasks. Each agent lives
in its own folder with a profile (`.md`) and a configuration file (`config.json`).

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
- IAM permissions scoped to the specific service (see each agent's profile)

## Two Ways to Use

### Option A: CAO install-time substitution (recommended)

Edit `config.json` to set your values, then install with `--env` flags:

```bash
cao install examples/aws/sqs-monitor/sqs-monitor-agent.md \
  --env AWS_PROFILE=my-profile \
  --env AWS_REGION=us-west-2 \
  --env QUEUE_URL=https://sqs.us-west-2.amazonaws.com/111111111111/my-queue
```

The `${VAR}` placeholders in the `.md` are resolved at install time. The
installed agent is fully baked with your values.

### Option B: Runtime config (self-contained)

The agent reads `config.json` from its folder at execution time via `jq`.
Edit `config.json` directly. No reinstall needed when values change.

```bash
# Edit the config
vim examples/aws/sqs-monitor/config.json

# Install the agent (placeholders remain as-is, agent reads config at runtime)
cao install examples/aws/sqs-monitor/sqs-monitor-agent.md
```

## Configuration

Each `config.json` uses a **flat key structure** for easy editing:

```json
{
  "profile": "my-aws-profile",
  "region": "us-east-1",
  "queue_url": "https://sqs.us-east-1.amazonaws.com/123456789012/MyQueue"
}
```

The same keys map 1:1 to `${VAR}` placeholders in the `.md` (uppercased):
`profile` → `${AWS_PROFILE}`, `region` → `${AWS_REGION}`, etc.

## Security Notes

- All agents use explicit `--profile` flags, never default credentials
- Use IAM roles with least-privilege permissions
- The `dynamodb-delete` agent performs destructive operations: test in dev first
- Never store real credentials in agent profile files or config.json
