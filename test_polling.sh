#!/bin/bash
# Quick test script for issue #80 inbox polling

set -e

echo "=== Testing Issue #80: Inbox Polling ==="
echo

# Check if server is running
if ! curl -s http://localhost:9889/health > /dev/null 2>&1; then
    echo "❌ CAO server is not running. Start it with: cao-server"
    exit 1
fi
echo "✅ CAO server is running"

# Install agent profiles if needed
echo "Installing agent profiles..."
cao install developer 2>/dev/null || true
cao install reviewer 2>/dev/null || true

# Create session with first terminal
echo "Creating session with first terminal..."
RESPONSE1=$(curl -s -X POST "http://localhost:9889/sessions?provider=kiro_cli&agent_profile=developer&session_name=test-session")
TERM1=$(echo "$RESPONSE1" | jq -r '.id // empty')
SESSION=$(echo "$RESPONSE1" | jq -r '.session_name // empty')

if [ -z "$TERM1" ]; then
    echo "❌ Failed to create terminal 1"
    echo "Response: $RESPONSE1"
    exit 1
fi

# Create second terminal in same session
echo "Creating second terminal..."
RESPONSE2=$(curl -s -X POST "http://localhost:9889/sessions/$SESSION/terminals?provider=kiro_cli&agent_profile=reviewer")
TERM2=$(echo "$RESPONSE2" | jq -r '.id // empty')

if [ -z "$TERM2" ]; then
    echo "❌ Failed to create terminal 2"
    echo "Response: $RESPONSE2"
    curl -s -X DELETE "http://localhost:9889/sessions/$SESSION" > /dev/null
    exit 1
fi

echo "Terminal 1: $TERM1"
echo "Terminal 2: $TERM2"

# Wait for terminals to initialize
echo "Waiting for terminals to initialize..."
sleep 5

# Send a message from TERM1 to TERM2
echo "Sending message from $TERM1 to $TERM2..."
MSG_RESPONSE=$(curl -s -X POST "http://localhost:9889/terminals/$TERM2/inbox/messages?sender_id=$TERM1&message=Test%20message%20for%20polling")
MSG_ID=$(echo "$MSG_RESPONSE" | jq -r '.message_id // empty')

if [ -z "$MSG_ID" ]; then
    echo "❌ Failed to send message"
    echo "Response: $MSG_RESPONSE"
    curl -s -X DELETE "http://localhost:9889/sessions/$SESSION" > /dev/null
    exit 1
fi

echo "Message ID: $MSG_ID"

# Wait for polling to deliver (should be within 2 seconds)
echo "Waiting for polling to deliver message..."
sleep 3

# Check message status
echo "Checking message status..."
MESSAGES=$(curl -s "http://localhost:9889/terminals/$TERM2/inbox/messages")
STATUS=$(echo "$MESSAGES" | jq -r ".[] | select(.id == $MSG_ID) | .status // empty")

if [ -z "$STATUS" ]; then
    echo "❌ FAILED: Message not found"
    echo "Messages: $MESSAGES"
    curl -s -X DELETE "http://localhost:9889/sessions/$SESSION" > /dev/null
    exit 1
fi

if [ "$STATUS" = "delivered" ]; then
    echo "✅ SUCCESS: Message was delivered via polling!"
    echo "   Status: $STATUS"
else
    echo "❌ FAILED: Message not delivered"
    echo "   Status: $STATUS"
    curl -s -X DELETE "http://localhost:9889/sessions/$SESSION" > /dev/null
    exit 1
fi

# Cleanup
echo "Cleaning up test session..."
curl -s -X DELETE "http://localhost:9889/sessions/$SESSION" > /dev/null

echo
echo "=== Test Complete ==="
