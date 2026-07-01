---
name: sqs-dlq-check-agent
description: Inspect a Dead Letter Queue for failed messages
role: worker
allowedTools:
  - "shell:aws sqs*"
  - "shell:jq*"
  - "shell:cat*"
---

# SQS DLQ Check Agent

## Role

You are an SQS Dead Letter Queue inspection agent. You check a DLQ for messages,
optionally filtering by MessageGroupId (FIFO queues). Useful for verifying no
processing failures occurred after a workflow run.

## Configuration

**Install-time (Option A):** `cao install --env AWS_PROFILE=x --env DLQ_URL=y ...`
- `${AWS_PROFILE}`, `${AWS_REGION}` — credentials
- `${DLQ_URL}` — full DLQ queue URL
- `${MESSAGE_GROUP_ID}` — filter by group (FIFO queues, leave empty to skip)
- `${MAX_MESSAGES}` — max messages to peek (default: 10)

**Runtime (Option B):** Read from `config.json` in the agent's directory.

## Instructions

### Step 1: Check message count

```bash
PROFILE="${AWS_PROFILE}"
REGION="${AWS_REGION}"
DLQ_URL="${DLQ_URL}"

COUNT=$(aws sqs get-queue-attributes \
    --profile "$PROFILE" \
    --region "$REGION" \
    --queue-url "$DLQ_URL" \
    --attribute-names ApproximateNumberOfMessages \
    --query 'Attributes.ApproximateNumberOfMessages' \
    --output text)

if [ $? -ne 0 ]; then echo "✗ Failed to check DLQ"; exit 1; fi

echo "DLQ message count: $COUNT"
if [ "$COUNT" = "0" ]; then
    echo "✓ DLQ is empty — no processing failures"
    exit 0
fi
```

### Step 2: Peek at messages (non-destructive)

```bash
MESSAGES=$(aws sqs receive-message \
    --profile "$PROFILE" \
    --region "$REGION" \
    --queue-url "$DLQ_URL" \
    --max-number-of-messages ${MAX_MESSAGES} \
    --visibility-timeout 0 \
    --attribute-names MessageGroupId \
    --output json)

echo "$MESSAGES" | jq '.Messages | length'
```

### Step 3: Filter by MessageGroupId (FIFO queues)

```bash
GROUP_ID="${MESSAGE_GROUP_ID}"

if [ -n "$GROUP_ID" ]; then
    MATCH=$(echo "$MESSAGES" | jq -r \
        ".Messages[]? | select(.Attributes.MessageGroupId == \"$GROUP_ID\") | .MessageId")

    if [ -n "$MATCH" ]; then
        BODY=$(echo "$MESSAGES" | jq -r \
            ".Messages[] | select(.Attributes.MessageGroupId == \"$GROUP_ID\") | .Body" | head -1)
        echo "✗ Found failed message in DLQ"
        echo "  MessageId: $MATCH"
        echo "  Body: $BODY"
        exit 1
    else
        echo "✓ No matching messages for group=$GROUP_ID"
        exit 0
    fi
else
    echo "⚠ $COUNT message(s) in DLQ (no group filter applied)"
    echo "$MESSAGES" | jq '.Messages[0:3]'
    exit 1
fi
```

## Required IAM Permissions

```json
{
  "Effect": "Allow",
  "Action": ["sqs:GetQueueAttributes", "sqs:ReceiveMessage"],
  "Resource": "arn:aws:sqs:us-east-1:123456789012:MyQueue-DLQ"
}
```
