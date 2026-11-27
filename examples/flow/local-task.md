---
name: local-task-demo
schedule: "*/10 * * * *"
agent_profile: developer
script: ./gate.sh
---

Append one line to `[[log_file]]` containing exactly this timestamp: [[timestamp]].
Create the file (and its parent directory) if it doesn't exist yet. Then reply
with only the line you appended.
