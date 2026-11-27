---
name: mock-factory-agent
description: Inert mock_cli agent standing in for the factory supervisor and the three team managers when the robotics replay trace is recorded. No model, no tools, no credentials.
provider: mock_cli
---

Inert. The mock_cli binary does not read this prompt. The recorder
(`record_robotics_trace.py`) plays the supervisor and manager decisions and makes the same CAO
calls (`assign`, `handoff`, a permission prompt) that real agents would make.
