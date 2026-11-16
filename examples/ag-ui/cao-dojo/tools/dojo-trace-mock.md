---
name: dojo-trace-mock
description: Keyless twin of dojo-approver for the CAO Dojo trace check. Runs the mock_cli test binary; no model, no tools, no credentials.
provider: mock_cli
---

Inert. The mock_cli binary does not read this prompt. The trace recorder
(`record_trace.py --mock`) types `__mock_approval__ <text>` into the terminal,
which raises a scripted approval prompt that CAO turns into an `approval_card`
interrupt.
