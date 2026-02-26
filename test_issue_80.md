# Manual Test for Issue #80 - Inbox Polling

## Setup

1. Start the CAO server:
```bash
cao-server
```

2. In another terminal, launch two agents:
```bash
cao launch --agents developer,reviewer --yolo
```

3. Wait for both agents to become idle (sitting at prompt)

## Test Scenario

### Step 1: Get Terminal IDs
```bash
# List all terminals
curl http://localhost:9889/terminals | jq
```

Note the terminal IDs for both agents (e.g., `term-abc123` and `term-def456`)

### Step 2: Send Message to Idle Agent
```bash
# Send a message from developer to reviewer (replace with actual IDs)
curl -X POST "http://localhost:9889/terminals/REVIEWER_TERMINAL_ID/inbox/messages?sender_id=DEVELOPER_TERMINAL_ID&message=Hello%20from%20developer"
```

### Step 3: Verify Delivery

**Expected behavior:**
- Within 2 seconds, the message should appear in the reviewer's terminal
- The reviewer agent should respond to the message

**Check message status:**
```bash
# Get inbox messages for the receiver
curl "http://localhost:9889/terminals/REVIEWER_TERMINAL_ID/inbox/messages" | jq
```

The message status should be `DELIVERED` (not stuck in `PENDING`)

### Step 4: Verify Polling Logs

Check the server logs for polling activity:
```bash
# Look for polling thread messages
grep -i "polling" ~/.cao/logs/server-*.log | tail -20
```

You should see:
- "Started inbox polling thread" when server starts
- Periodic delivery attempts (if there are pending messages)

## Success Criteria

✅ Message is delivered to idle agent within 2 seconds
✅ Message status changes from PENDING to DELIVERED
✅ Agent receives and can respond to the message
✅ No manual intervention needed (no need to type in terminal to trigger delivery)

## Failure Scenario (Before Fix)

Without the polling fix:
- Message would stay in PENDING status indefinitely
- Agent would never receive the message
- Only way to trigger delivery would be to type something in the agent's terminal to create log output
