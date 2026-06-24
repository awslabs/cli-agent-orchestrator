# OMP CLI Provider

CAO can launch the OMP CLI (`omp`) as a built-in provider. OMP is integrated as
a generic TUI agent: its role context reaches the agent through the install-time
context file, and status detection uses environment-overridable regexes so the
patterns can be recalibrated against real `omp` output without a code change.

## Prerequisites

- The OMP CLI is installed and authenticated.
- The `omp` binary is on `PATH`. CAO resolves it with `shutil.which("omp")` and
  raises a provider error at launch time if it is missing.

```bash
which omp
```

## CAO Profile

Create a CAO agent profile that selects the OMP provider:

```yaml
---
name: omp_default
description: Developer backed by OMP CLI
provider: omp_cli
role: developer
---

You are a helpful developer agent.
```

An optional `model` is read from the agent profile (or set explicitly) and
forwarded as `--model` at launch.

## Installation behaviour

OMP has no native agent-config format yet, so installation is
**context-file-only**: CAO writes the profile's role body to the standard
context file, which `omp` reads at startup. No per-provider agent file is
written. This is the same lowest-friction path the early-stage
`claude_code` / `hermes` providers took.

## Capability set (intentionally minimal)

For now OMP is deliberately excluded from both `terminal_service` capability
sets:

- **Runtime skill prompts** — skills reach OMP via the context file, not a
  launch-time system prompt, so there is no `skill_prompt` to consume.
- **Soft tool enforcement** — OMP's native tool vocabulary is not yet
  characterised, so there is no reliable advisory prompt to emit. Tool
  restrictions are still recorded but only reach the agent via the context file.

Both decisions should be revisited once OMP's native tool / approval model is
confirmed.

## Status detection

Status detection classifies the terminal buffer into IDLE, PROCESSING,
COMPLETED, WAITING_USER_ANSWER, ERROR, and UNKNOWN. The agent distinguishes a
fresh spawn (IDLE) from a delivered turn (COMPLETED) via an internal turn
counter incremented on every `send_input`; the buffer alone cannot tell them
apart.

All detection patterns are overridable via environment variables:

| Env var | Purpose |
|---|---|
| `CAO_OMP_IDLE_PROMPT_REGEX` | Idle prompt line (wait for next message). |
| `CAO_OMP_IDLE_LOG_REGEX` | Idle prompt substring used for log monitoring. |
| `CAO_OMP_PROCESSING_REGEX` | Spinner / in-flight indicator while generating. |
| `CAO_OMP_WAITING_REGEX` | Approval / selection dialog that blocks the agent. |
| `CAO_OMP_USER_PREFIX_REGEX` | User message marker (response extraction). |
| `CAO_OMP_ASSISTANT_HEADER_REGEX` | Assistant message marker (response extraction). |

The defaults are placeholders calibrated against representative TUI agents.
Capture real `omp` output and set the env vars to refine detection — no source
change required.

## Known limitations

- **Unknown real output format.** The default regexes are generic placeholders;
  they must be calibrated against real `omp` output before OMP is used in
  production orchestration.
- **Advisory tool restrictions.** No native tool-blocking flag; restrictions are
  prompt-level only.
- **`omp` is a short name.** `shutil.which` resolves the first `omp` on `PATH`;
  ensure the intended OMP binary is the one found.
