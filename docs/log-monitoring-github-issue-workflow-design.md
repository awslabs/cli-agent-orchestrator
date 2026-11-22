# CAO Log Monitoring and GitHub Issue Workflow

## 1. Purpose

This design describes a CAO workflow that periodically monitors configured log
locations, determines whether an observed error requires a source-code fix, and
creates a GitHub Issue when the finding passes the safety and deduplication
gates.

The workflow creates Issues only. It does not modify source code, create pull
requests, or allow an analysis agent to create Issues directly.

## 2. Design principles

- Reuse the existing `cao schedule` scheduler and flow daemon for periodic execution.
- Treat log locations and related source-code locations as explicit configuration.
- Read only newly appended log data when possible; handle rotation and truncation safely.
- Use deterministic rules to identify candidates, then use a CAO read-only agent for triage.
- Keep GitHub publication server-owned and separate from agent reasoning.
- Fail closed on invalid configuration, secrets, ambiguous triage, and duplicate findings.
- Persist cursors, fingerprints, and publication state so a restart does not lose safety state.

## 3. Runtime architecture

```text
scheduled monitor flow
  -> validate configuration and acquire monitor lock
  -> read new log data using per-source cursors
  -> group multiline errors and calculate normalized fingerprints
  -> apply occurrence, lookback, and ignore-pattern rules
  -> collect bounded context from configured source-code roots
  -> run CAO read-only triage agent
  -> validate triage JSON
  -> apply confidence, evidence, secret, and deduplication gates
  -> publish an Issue through the GitHub API
  -> persist cursor, finding, and publication state
```

The current `cao workflow run` surface is primarily operator-triggered. The
periodic monitor therefore uses a new `kind: log_issue_monitor` scheduled flow
handled by the existing flow daemon. The monitor runtime uses CAO's shared
`run_agent_step` substrate for agent analysis.

## 4. Monitor configuration

Monitor definitions are Markdown files with YAML front matter, for example:

`~/.aws/cli-agent-orchestrator/monitors/order-api.monitor.md`

```yaml
---
name: order-api-error-monitor
kind: log_issue_monitor
schedule: "*/5 * * * *"
provider: codex
agent_profile: log_triage_reviewer

monitor:
  log_paths:
    - id: api
      paths:
        - /var/log/order-api/api.log
        - /var/log/order-api/api.log.*
      format: plain
      max_bytes_per_run: 131072

  source_code:
    roots:
      - local_path: /workspace/order-api
        repo_path: .
        include:
          - src/**/*.py
          - config/**/*.yaml
        exclude:
          - "**/vendor/**"
          - "**/.venv/**"

  state_path: ~/.aws/cli-agent-orchestrator/monitor-state/order-api.json

  detection:
    min_occurrences: 2
    lookback_seconds: 900
    ignore_patterns:
      - "health check timeout during deployment"

  triage:
    confidence_threshold: 0.85
    max_log_context_bytes: 12000
    max_source_context_bytes: 24000

  github:
    repository: acme/order-api
    token_env: CAO_GITHUB_TOKEN_ORDER_API
    labels:
      - bug
      - cao-monitor
    create_mode: automatic
    reopen_closed: false
---

Determine whether the detected error requires a source-code fix.
Return JSON only. Never modify files or create GitHub Issues directly.
```

### 4.1 Configuration contract

| Field | Required | Description |
|---|---:|---|
| `name` | yes | Stable monitor identifier. |
| `kind` | yes | Must be `log_issue_monitor`. |
| `schedule` | yes | Existing CAO cron expression. |
| `provider` | yes | CAO provider used for triage. |
| `agent_profile` | yes | Read-only triage profile. |
| `monitor.log_paths` | yes | One or more named file, directory, or glob sources. |
| `monitor.source_code.roots` | yes | Local source roots and repository-relative mappings. |
| `monitor.state_path` | no | Cursor/state location; defaults under CAO home. |
| `monitor.detection` | no | Candidate and recurrence policy. |
| `monitor.triage` | no | Context limits and confidence threshold. |
| `monitor.github` | yes for publishing | Repository, token environment variable, labels, and publication policy. |

Path values are resolved with `realpath`. Concrete log and source paths must
be readable, and source roots must exist during validation. A log file may be
temporarily absent because of rotation. Glob patterns must have a valid,
readable parent directory. Resolved paths must not point to blocked system
directories or escape their configured read root through a symlink.

The configuration is the only authority for paths. Paths extracted from log
content or returned by the triage agent are never used as new filesystem
targets.

## 5. Detection and state handling

### 5.1 Cursor handling

Each log source stores:

- canonical path and file identity/inode when available;
- byte offset;
- last observed size and timestamp;
- rotation/truncation generation.

When the inode changes or the file size moves backwards, the reader treats the
source as rotated or truncated and starts from the configured safe position.
The initial read position defaults to the end of the current file to avoid
creating Issues for an unbounded historical backlog.

All reads are bounded by `max_bytes_per_run`.

### 5.2 Error grouping and fingerprinting

The detector groups multiline traceback/error blocks using configured format
rules and common continuation markers. It then normalizes unstable values such
as timestamps, UUIDs, request IDs, memory addresses, and numeric identifiers.

The normalized result becomes the finding fingerprint. A finding record tracks:

- first and last observed time;
- occurrence count;
- source IDs;
- current processing state;
- triage result;
- GitHub Issue number/URL, if published.

The default candidate gate requires at least two occurrences within fifteen
minutes. Both values are configurable.

## 6. Source-code context collection

The context collector uses only configured `source_code.roots`.

It maps stack-trace paths to the configured `repo_path`, applies include and
exclude globs, and reads a bounded number of bytes. If a log does not identify
a file, the collector provides the configured source manifest and the most
relevant bounded excerpts found under the source roots.

Absolute local paths are never placed in the Issue body. References are emitted
as repository-relative paths with optional line numbers.

Source files are read-only. The monitor does not run arbitrary commands based on
log content and does not pass source paths through a shell.

## 7. CAO triage agent

The triage agent is executed through `run_agent_step` with:

- a dedicated read-only profile;
- `fs_read` and `fs_list` only;
- no shell execution;
- no network access;
- no file-writing capability;
- a configured working directory where required by the provider.

The prompt contains the normalized finding, bounded log context, source
context, repository mapping, and explicit instructions to return JSON only.

The server validates the response against this contract:

```json
{
  "decision": "create_issue|ignore|needs_review",
  "confidence": 0.91,
  "severity": "critical|high|medium|low",
  "title": "short issue title",
  "summary": "why this is a defect",
  "root_cause": "suspected cause",
  "affected_files": [
    {"path": "src/example.py", "line": 42}
  ],
  "evidence": ["bounded evidence only"],
  "fingerprint": "normalized fingerprint"
}
```

Invalid JSON, a mismatched fingerprint, missing evidence, or an unsupported
decision is treated as `needs_review`; it never creates an Issue.

## 8. Issue publication policy

Publication is performed by a CAO server-side `GitHubIssuePublisher`, not by
the agent. The publisher calls the GitHub API directly over HTTPS. It does not
invoke the `gh` shell command.

An Issue is created only when all of these conditions hold:

- the configured occurrence threshold is met;
- triage returns `decision=create_issue`;
- confidence is at least the configured threshold, default `0.85`;
- at least one affected source location or reproducible evidence exists;
- the content passes the existing secret gate;
- no matching open or recent Issue exists.

The Issue body starts with a stable marker:

```text
<!-- cao-monitor:fingerprint=<normalized-fingerprint> -->
```

The body contains the monitor name, occurrence window, error summary, suspected
root cause, repository-relative source references, and redacted evidence. It
does not contain credentials, unrestricted log dumps, or absolute local paths.

The GitHub token is read from the configured environment variable and is never
stored in the monitor file, state file, database, agent prompt, or Issue body.

### 8.1 Duplicate and failure handling

- Search for the fingerprint marker before creating an Issue.
- If an Issue POST times out, search for the marker before retrying.
- Never blindly retry a non-idempotent Issue creation request.
- Persist an in-flight publication state so a server restart can reconcile it.
- Rate limits and API failures leave the finding eligible for a later retry.
- Closed Issues are not reopened by default; this is configurable.

## 9. Operator interface

Add monitor-specific commands backed by the existing scheduling and flow state:

```bash
cao monitor validate <file>
cao monitor add <file>
cao monitor list
cao monitor run <name> --dry-run
cao monitor status <name>
cao monitor enable <name>
cao monitor disable <name>
```

`--dry-run` performs log reading, fingerprinting, context collection, and
triage, but never calls the GitHub Issue creation endpoint.

The status surface should expose only metadata:

- last and next run;
- last cursor update;
- candidate count;
- last fingerprint;
- triage decision and confidence;
- deduplicated/created/blocked state;
- last failure category.

Raw log and source contents must not be included in status output.

## 10. Implementation changes

### Models and configuration

- Add typed monitor configuration models and validation.
- Extend the scheduled flow model with the monitor kind and validated monitor metadata.
- Reuse CAO path validation and secret-gate helpers.

### Runtime services

- Add a monitor service for cursor reads, grouping, fingerprinting, context collection, and policy evaluation.
- Add durable monitor state and publication records with unique monitor/fingerprint constraints.
- Extend the existing flow daemon to dispatch monitor flows without creating a long-lived conductor session.
- Add a read-only triage adapter around `run_agent_step`.
- Add a GitHub API publisher with marker-based reconciliation and bounded timeouts.

### Documentation and CLI

- Add monitor authoring and operation documentation.
- Add validation, dry-run, status, and lifecycle commands.
- Document token setup without placing tokens in configuration files.

## 11. Verification plan

### Unit tests

- Missing fields, invalid types, invalid cron, invalid paths, blocked paths, and symlink escapes.
- Initial-tail behavior, incremental cursor reads, rotation, truncation, and byte limits.
- Multiline traceback grouping and fingerprint normalization.
- Ignore patterns, occurrence threshold, lookback window, and confidence gate.
- Invalid triage JSON, mismatched fingerprint, timeout, and unsupported decision.
- Secret detection and redaction before agent and GitHub publication.
- Repository-relative source path mapping.

### Integration tests

- Fake log directory plus fake source tree and stub CAO triage result.
- Dry-run with zero GitHub calls.
- Successful Issue creation with the expected marker and labels.
- Duplicate fingerprint with an existing open Issue.
- POST timeout followed by marker reconciliation.
- GitHub rate limit and server restart recovery.
- Concurrent scheduler ticks proving one monitor run lock.

### Acceptance criteria

1. A configured monitor reads only new log data on each scheduled run.
2. The same recurring error produces at most one active Issue per fingerprint.
3. Invalid, low-confidence, secret-bearing, or ambiguous findings do not create Issues.
4. A GitHub API outage does not lose the finding or create blind duplicates.
5. Source code is never modified by the monitoring workflow.
6. All configured log and source-code locations are visible in validation output and status metadata.
7. `--dry-run` is safe to run against production logs.

## 12. Defaults and assumptions

- Monitor configuration is per workflow file; global settings contain only optional defaults.
- Hybrid rule-based detection plus CAO AI triage is the default.
- Automatic Issue creation requires the safety gates described above.
- The monitor creates Issues only; remediation and pull requests remain human-controlled.
- The existing CAO scheduler is reused instead of adding a second scheduling system.
