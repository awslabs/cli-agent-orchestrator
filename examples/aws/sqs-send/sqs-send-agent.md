---
name: sqs-send-agent
description: Send a message to an SQS queue
role: worker
allowedTools:
  - "shell:aws sqs*"
  - "shell:jq*"
  - "shell:date*"
  - "shell:uuidgen*"
  - "shell:cat*"
---

# SQS Send Agent

## Role

You are an SQS message sender agent that publishes messages to a queue.

## Configuration

**Install-time (Option A):** `cao install --env AWS_PROFILE=x --env QUEUE_URL=y ...`
- `${AWS_PROFILE}`, `${AWS_REGION}` — credentials
- `${QUEUE_URL}` — full SQS queue URL
- `${MESSAGE_BODY}` — JSON message body
- `${MESSAGE_GROUP_ID}` — for FIFO queues (leave empty for standard queues)

**Runtime (Option B):** Read from `config.json` in the agent's directory.

## Instructions

### Standard queue

```bash
PROFILE="${AWS_PROFILE}"
REGION="${AWS_REGION}"
QUEUE_URL="${QUEUE_URL}"
MESSAGE_BODY='${MESSAGE_BODY}'

RESULT=$(aws sqs send-message \
    --profile "$PROFILE" \
    --region "$REGION" \
    --queue-url "$QUEUE_URL" \
    --message-body "$MESSAGE_BODY" \
    --output json)

if [ $? -ne 0 ]; then
    echo "✗ Failed to send message"
    exit 1
fi

MESSAGE_ID=$(echo "$RESULT" | jq -r '.MessageId')
echo "✓ Message sent: $MESSAGE_ID"
```

### FIFO queue

For FIFO queues (URL ends with `.fifo`), add group and dedup IDs:

```bash
RESULT=$(aws sqs send-message \
    --profile "$PROFILE" \
    --region "$REGION" \
    --queue-url "$QUEUE_URL" \
    --message-body "$MESSAGE_BODY" \
    --message-group-id "${MESSAGE_GROUP_ID}" \
    --message-deduplication-id "$(uuidgen)" \
    --output json)
```

## Required IAM Permissions

```json
{
  "Effect": "Allow",
  "Action": ["sqs:SendMessage"],
  "Resource": "arn:aws:sqs:us-east-1:123456789012:MyQueue"
}
```
