# Provider error strings mined from transcripts, headless runs and conductor state

Read-only survey. Nothing outside the scratchpad was modified.

## Corpus actually searched

| Corpus | Size | What it yielded |
|---|---|---|
| `~/.claude/projects/*/*.jsonl` | 442 files (441 after excluding this session's own transcript), 1.0G | **Everything.** 61 originating terminal error events + 64 internal auto-retry events |
| `/var/folders/.../T/*-headless/*/` | **113** dirs across **7** providers (not 72/3 — also `deepseek-claude-`, `muse-`, `prime-agent-`, `zai-claude-`) | 4 distinct failure surfaces; confirms the rendered form of two JSONL strings |
| `~/.local/state/cao-conductor/*/runs/*/` | 334 run dirs, 713M | **Zero verbatim provider error strings.** Orchestrator-level failure taxonomy only |

Method: streamed/parsed all 442 JSONL files with Python (not sampled), keyed on `isApiErrorMessage`, `type:"system"/subtype:"api_error"`, `subtype:"model_refusal_fallback"`, `apiErrorStatus`, `apiErrorIsTransient`, `stop_reason`, and `is_error` tool results; aggregated by digit-normalised message. Headless and conductor corpora swept with ripgrep across every file, then contexts read.

**Self-exclusion:** this task's own session transcript (`-Users-colin/b1d114e9-….jsonl`) contains the task prompt, which quotes the canonical string. It is excluded from every count below. Left in, it would have inflated the headline finding by echoing my own instructions back as evidence.

---

## The single most useful discovery

Claude Code writes **two different kinds** of API-error record, and only one of them ever reaches the screen:

- `type:"system", subtype:"api_error"` — an **internal retry**, with `retryInMs`, `retryAttempt`, `maxRetries: 10`. 64 of these. **These never render as a terminal error** and must not trigger a nudge; the harness is already recovering. Retry-attempt histogram: attempt 1 = 18, attempts 2–10 = 5 each, i.e. 5 request sequences burned all ten retries and only *then* surfaced to the user.
- `isApiErrorMessage: true` on an assistant message — the **rendered, terminal** error. 61 of these. This is what the detector sees.

Each `isApiErrorMessage` event also carries a machine-readable `error` code (`server_error` / `billing_error` / `rate_limit` / `authentication_failed` / `model_not_found`) and often `apiErrorStatus`. The 61 split exactly: 35 `server_error`, 11 `billing_error`, 10 `rate_limit`, 3 `authentication_failed`, 3 `model_not_found` (counts include this session's one excluded event).

`apiErrorIsTransient` exists but is populated on only 8 events — all `rate_limit`/429, all set to **`false`**. There is no observed case of it being `true`, so it is a usable negative signal (do not nudge) and useless as a positive one.

---

## Findings, by occurrence count descending

Counts are **originating events** unless stated. "Re-quotes" = the same string later appearing inside prose, a tool result, or a sub-agent report; these are real detector inputs too, so I give them separately rather than folding them in.

### 1. Connection closed mid-response — the canonical case

| | |
|---|---|
| Provider | Anthropic (Claude Code); also reproduced through the Claude Code harness on DeepSeek |
| Verbatim | `API Error: Connection closed mid-response. The response above may be incomplete.` |
| Regex | `r"API Error:\s*Connection closed mid-response"` |
| Action | **nudge** |
| Count | **28 originating terminal events**; 56 total textual occurrences; +1 observed in a headless `final.txt` |
| Evidence | `/Users/colin/.claude/projects/-Users-colin--secrets-env/c4063b7a-1fd4-4795-b1a6-a9718e5fdedb.jsonl:307`; rendered form at `/var/folders/cm/v5tlfz_d4wbg9mxjsppm32600000gn/T/deepseek-claude-headless/20260807-013127-49004-14326/final.txt` |

Reasoning: `error: "server_error"`, no HTTP status, no rate-limit payload — the stream died mid-flight after the model had already produced useful output, so the work is intact and resumable by continuing.

This is the only string in the corpus **observed in all three forms**: as a structured JSONL event, as the last line of a rendered `final.txt`, and re-quoted inside a parent agent's failure summary. That triple confirmation is why it is the safest possible anchor for the detector.

### 2. Rate / usage limit (Anthropic and Z.ai families)

| | |
|---|---|
| Provider | Anthropic; Z.ai GLM via the Claude Code shim (`[1302]`/`[1308]` codes are Z.ai's) |
| Verbatim | `API Error: Request rejected (429) · [1308][Usage limit reached for 5 hour. Your limit will reset at 2026-07-27 14:28:00][<redacted request id>]` |
| Regex | `r"Usage limit reached for \d+\s*hour.*?reset at\s*(?P<reset>[\d\-: ]+)"` |
| Action | **wait** (reset timestamp is in the string — parse it) |
| Count | 8 originating terminal events; **51 additional internal retry events**; 120 total textual occurrences |
| Evidence | `/Users/colin/.claude/projects/-Users-colin-Projects-cao-conductor-worktrees-final-gate-activation/60a87d05-4282-4b3c-9e74-3a4ac28c135e.jsonl:1143` |

Reasoning: `apiErrorIsTransient: false` and a stated reset time — nudging before the reset just burns another rejected request.

Two sibling forms, same action:

- `You've hit your weekly limit · resets Jul 27 at 6pm (America/New_York)` — 1 event, `429`. Regex `r"You've hit your weekly limit\s*·\s*resets\s*(?P<reset>.+)"`. **wait**, but a weekly window may exceed any sane orchestrator timeout, so treat as **escalate** if the reset is more than a few hours out. Evidence: `…-chess-shakedown-worktrees-pr07-expert-spec/b1867ef2-….jsonl:1786`.
- `You've reached your Fable 5 limit. Run /usage-credits to continue or switch models with /model.` — 1 event, `429`. Regex `r"You've reached your .+ limit\. Run /usage-credits"`. **route** — no reset time offered, only "switch models". Evidence: `…-worktrees-pre-chess-orchestration-spec/d2b908e4-….jsonl:2523`.
- `429 [1302][Rate limit reached for requests][<redacted request id>]` — 1 internal retry event, Z.ai. Regex `r"\[1302\]\[Rate limit reached for requests\]"`. **nudge** (short-window throttle, distinct from `[1308]` quota exhaustion).

### 3. Sub-agent terminated early (wrapper form)

| | |
|---|---|
| Provider | Anthropic (Claude Code Task/Agent tool) |
| Verbatim | `Agent terminated early due to an API error: API Error: Connection closed mid-response. The response above may be incomplete.` |
| Regex | `r"Agent terminated early due to an API error:\s*(?P<inner>.+)"` |
| Action | inherit from `<inner>` (**nudge** for connection-closed, **wait** for the 429 form) |
| Count | **22** |
| Evidence | `/Users/colin/.claude/projects/-Users-colin/e1c64300-9510-4715-aee0-7d4ee8f990fc.jsonl:571`; multi-agent batch at `/Users/colin/.claude/projects/-private-tmp-cr-232-2026-07-18-17-33-52/026b9a8d-….jsonl:140-156` |

Reasoning: the parent survives and reports the child's death, so the detector must unwrap rather than match the outer sentence — the outer text is identical whether the inner cause is nudgeable or a quota wall. Observed wrapping both the connection-closed string and the 429 usage-limit string.

### 4. Credit balance too low

| | |
|---|---|
| Provider | Anthropic (API-key billing path) |
| Verbatim | `Credit balance is too low` |
| Regex | `r"Credit balance is too low"` |
| Action | **escalate** |
| Count | 11 originating events (`error: "billing_error"`, status `400`); 35 total textual occurrences |
| Evidence | `/Users/colin/.claude/projects/-Users-colin--claude-jobs-40b3fe04-tmp-efftest/095eb680-3105-451c-afb3-cde84dd7667c.jsonl:10` |

Reasoning: needs a human to add funds; no amount of waiting or nudging clears it. Note all 11 fired at line 10 of their sessions — this fails at launch, so the detector should catch it as a startup guard, not a mid-run stall.

### 5. Connection reset (internal retry only)

| | |
|---|---|
| Provider | Anthropic |
| Verbatim | `Unable to connect to API (ECONNRESET)` (message: `Connection error.`) |
| Regex | `r"Unable to connect to API \((?P<code>ECONNRESET|ENOTFOUND|ETIMEDOUT|ECONNREFUSED)\)"` |
| Action | **nudge** — but only if it reaches the pane |
| Count | **13, all `system/api_error` internal retries — 0 terminal** |
| Evidence | `/Users/colin/.claude/projects/-Users-colin-Projects-ai-traffic-proxy/c153808a-52fb-4e47-94a1-7a3ef530056c.jsonl:29` |

Reasoning: the harness retried all 13 and recovered every time. A detector that watches a log stream rather than the pane would fire 13 false positives here. The `ENOTFOUND` variant *did* reach terminal once (below), so the same string can be either — the discriminator is which event type carries it, not the text.

### 6. Model not available

| | |
|---|---|
| Provider | Muse (`muse-spark-1.2`) and DeepSeek (`deepseek-v4-flash`), both via the Claude Code harness |
| Verbatim | `There's an issue with the selected model (muse-spark-1.2). It may not exist or you may not have access to it. Run --model to pick a different model.` |
| Regex | `r"There's an issue with the selected model \((?P<model>[^)]+)\)"` |
| Action | **route** |
| Count | 3 originating events (status `404`); 11 total textual occurrences |
| Evidence | `/Users/colin/.claude/projects/-private-var-folders-…-T-tmp-BL4iFD69TP/657c13b5-….jsonl:10`; `/Users/colin/.claude/projects/-private-tmp-fm-wd2/137fde77-….jsonl:16` |

Reasoning: a nudge re-sends to the same missing model. Note the trailing hint varies (`Run --model` vs `Run /model`) — do not anchor the regex on it.

### 7. Authentication failures

| | |
|---|---|
| Provider | Z.ai / DeepSeek (API-key paths) and Anthropic (OAuth path) |
| Action | **escalate** — all forms |

| Verbatim | Regex | Count | Evidence |
|---|---|---|---|
| `Failed to authenticate. API Error: 401 Authentication Fails, Your api key: <redacted> is invalid` | `r"Failed to authenticate\. API Error:\s*401"` | 1 event, 3 textual | `-Users-colin-Projects-cao-conductor/9f11c873-….jsonl:10`; rendered at `…/T/claude-headless/20260806-153103-82781-31069/final.txt` |
| `Please run /login · API Error: 401 token expired or incorrect` | `r"API Error:\s*401 token expired or incorrect"` | 1 | `…-dnd-scheduler-worktrees-cao-canary-zai-edit-20260717/a4706bc8-….jsonl:16` |
| `Login expired · Please run /login` | `r"Login expired\s*·\s*Please run /login"` | 1 | `-Users-colin-Projects-aegix/3ced734d-….jsonl:14` |

The key-bearing error prints a masked suffix; I have redacted it. Reasoning: every form names a human action (`/login`, replace key).

### 8. Server-side 5xx

| Verbatim | Provider | Regex | Action | Count | Evidence |
|---|---|---|---|---|---|
| `API Error: 500 Internal server error. This is a server-side issue, usually temporary — try again in a moment. If it persists, check https://status.claude.com.` | Anthropic | `r"API Error:\s*500 Internal server error"` | **nudge** | 2 | `…-worktrees-cond-0072-consumer-hardening/d1df5efc-….jsonl:106` |
| `API Error: Server error mid-response. The response above may be incomplete.` | Anthropic | `r"API Error:\s*Server error mid-response"` | **nudge** | 2 | `-Users-colin/e1c64300-….jsonl:1383` |
| `API Error: 529 [1305][The service may be temporarily overloaded, please try again later][<redacted request id>]. This is a server-side issue, usually temporary — try again in a moment. If it persists, check your inference gateway (api.z.ai).` | Z.ai | `r"API Error:\s*529\b|service may be temporarily overloaded"` | **nudge** | 1 | `-Users-colin-Projects-custom-skills-zai-claude-headless/c45fecc3-….jsonl:10` |
| `API Error: Unable to connect to API (ENOTFOUND)` | Anthropic | see §5 regex | **nudge** | 1 | `/Users/colin/.claude/projects/-private-tmp-cr-13-2026-07-17-02-51-28/0a73e795-….jsonl:36` |

Reasoning: all four self-describe as temporary and carry no quota semantics. The strings even embed the retry advice.

### 9. Fable 5 safety-classifier fallback

| | |
|---|---|
| Provider | Anthropic |
| Verbatim | `Fable 5's safeguards flagged this message. The safeguards are intentionally broad right now and may flag safe and routine coding, cybersecurity, or biology work. These measures let us bring you Mythos-level capabilities sooner, and we're working to refine them. Switched to Opus 4.8. Send feedback with /feedback or learn more: https://support.claude.com/en/articles/<id>` |
| Regex | `r"safeguards flagged this message.*?Switched to (?P<fallback>[\w.\- ]+)"` |
| Action | **route** — though the harness has *already* routed |
| Count | 3 (`system` / `subtype: model_refusal_fallback`, `level: "warning"`) |
| Evidence | `…-worktrees-control-plane-recovery-fable5-retry/9790cb83-….jsonl:649` |

Reasoning: level is `warning`, not `error`, and the harness auto-switched to Opus and continued — so this is **not** a stop condition and must not trigger a nudge. It is worth detecting only so an orchestrator knows the run silently changed model mid-flight, which invalidates any "this was produced by Fable 5" assumption.

### 10. Claude Code auto-mode permission classifier

| | |
|---|---|
| Provider | Anthropic (harness-side, not the model API) |
| Verbatim | `Permission for this action was denied by the Claude Code auto mode classifier. Reason: [<category>] <redacted rationale>. If you have other tasks that don't depend on this action, continue working on those. IMPORTANT: You *may* attempt to accomplish this action using other tools…` |
| Regex | `r"denied by the Claude Code auto mode classifier\.\s*Reason:\s*(?:\[(?P<category>[^\]]+)\])?"` |
| Action | **unknown** — leaning "no action" |
| Count | 3 distinct (8 textual), delivered as `is_error: true` tool results |
| Evidence | `-Users-colin-Projects-aegix-apps-aegix-desktop/07859689-….jsonl:2633`; `-Users-colin-Projects-aegix-apps-mobile/8ab73552-….jsonl:85`; `…-worktrees-fire-marshal-p1/b9033c5f-….jsonl:159` |

Observed categories: `[Credential Leakage]`, `[Credential Exploration]`, `[Irreversible Local Destruction]`, and a bare `Blocked by classifier.`. **I have redacted the rationale text** — one instance quotes a command containing a real GitHub PAT. Reasoning: this denies a single tool call, the agent keeps running, and the message explicitly instructs it to continue with other work. Nudging is harmless but pointless; the danger is a detector reading `is_error: true` and declaring the run dead.

### 11. `Execution error`

| | |
|---|---|
| Provider | Z.ai and DeepSeek, via the Claude Code harness |
| Verbatim | `Execution error` (entire contents of `final.txt`) |
| Regex | `r"\AExecution error\s*\Z"` |
| Action | **unknown** |
| Count | 3 runs, all `exit_code` 125 |
| Evidence | `…/T/zai-claude-headless/20260805-214239-25305-16389/final.txt`, `…/20260805-223630-97105-9784/final.txt`, `…/T/deepseek-claude-headless/20260807-084334-98528-15764/final.txt` |

Reasoning: two words, no cause, no stream file written. See the unclassified section.

### 12. Kimi OAuth resolution failure

| | |
|---|---|
| Provider | Moonshot Kimi |
| Verbatim | `error: failed to run prompt: internal: OAuth request to https://auth.kimi.com/api/oauth/token failed: fetch failed: getaddrinfo ENOTFOUND auth.kimi.com` followed by `See log: /Users/colin/.kimi-code/logs/kimi-code.log` |
| Regex | `r"error: failed to run prompt:\s*(?P<detail>.+)"` |
| Action | **nudge** (DNS failure is transient) — but see caveat |
| Count | 1, `exit_code` 1 |
| Evidence | `…/T/kimi-headless/20260806-012546-31990-27705/stderr.log` (tail) |

Reasoning: `getaddrinfo ENOTFOUND` is transient DNS, not a credential problem — but the Kimi CLI **exits the process**, so a nudge to a dead pane does nothing. This needs a relaunch, not a "continue". It is the clearest example in the corpus of *recoverable cause, unrecoverable mechanism*.

### 13. Harness supervisor loss (not a provider error)

`supervisor vanished without an exit sentinel; the calling host may have terminated detached children` — 6 occurrences in `stderr.log`, all `exit_code` 125, across kimi/zai/deepseek. Regex `r"supervisor vanished without an exit sentinel"`. **escalate** (local orchestration bug). Included because it co-occurs with `Execution error` and will otherwise be misread as a provider fault. Evidence: `…/T/kimi-headless/20260806-201541-13462-19269/stderr.log`.

### 14. Non-string stop condition: `stop_reason: "max_tokens"`

11 assistant messages across the corpus ended with `stop_reason: "max_tokens"` (versus 68,201 `tool_use`, 2,858 `end_turn`, 73 `stop_sequence`). This is a genuine mid-work stall that a "continue" resolves — and it produces **no error text at all**. A purely string-matching detector will never see it. Action: **nudge**. Detect via the structured field where available; on-screen it is indistinguishable from a normal turn ending.

---

## What I could not classify

- **`Execution error` (§11).** Three runs, exit 125, and the directories contain no `stream.jsonl` at all — the process died before writing one. `stderr.log` holds only benign startup warnings plus the supervisor-vanished line. I cannot tell whether the provider failed, the harness failed, or the host killed the process. Given exit 125 and the supervisor message, local orchestration is the better bet, but I have no evidence that separates it from a provider fault.
- **`apiErrorIsTransient` semantics.** Populated on 8 of 61 events, always `false`. I never observed `true`, so I cannot confirm it is even emitted for transient errors, and I would not build on it as a positive signal.
- **`[1302]` vs `[1308]` Z.ai codes.** I classified `1302` (rate limit) as nudge and `1308` (usage limit) as wait from the message text alone. Only `1308` was ever observed reaching a terminal state; `1302` appeared once, as an internal retry. The nudge classification for `1302` is inference from wording, not observation.
- **Whether a nudge actually works.** This is the largest gap. The corpus records errors, not recoveries. I found no case where a nudge was sent after one of these strings and the outcome logged, so every `nudge` verdict below is reasoned from error semantics — transient transport, server-side 5xx, no quota payload — and **not** from an observed successful resume. The 13 auto-recovered `ECONNRESET` retries are the closest thing to positive evidence, and those were recovered by the harness's own retry loop, not by a nudge.

### Strings I did NOT find, despite searching

Searched case-insensitively across all three corpora for every term in the brief. These produced **no local observation**:

- `overloaded_error`, `invalid_request_error`, `permission_error`, `not_found_error` as Anthropic API *type* strings — the harness rewrites them into prose before display.
- `502`, `503` from any provider. (`503`/`529` hits in the conductor corpus were all sha256 substrings, bundled JS, or design prose.)
- `context length` / `prompt is too long` / context-ceiling errors. The only token-limit string was tool-level: `File content (NNNNN tokens) exceeds maximum allowed tokens (NNNNN). Use offset and limit parameters…` (8 occurrences) — a Read-tool guard, not a provider error, action **nudge** (the agent retries with offsets on its own).
- `stop_reason: "refusal"` — zero occurrences, despite being the documented API-level refusal signal.
- **Safety/policy blocks were never locally observed.** `API Error: Claude Code is unable to respond to this request, which appears to violate our Usage Policy` (16 textual hits) and `API Error: 4NN Output blocked by content filtering policy` (21 textual hits) appear **only** inside one project's design discussion, and reading that discussion shows they were *web-research findings* citing `anthropics/claude-code#4210` and `#43703`, explicitly distinguished from "locally observed" strings by that agent. I am reporting them as **plausible but unverified here**; if the detector needs them, treat the wording as unconfirmed and match loosely: `r"appears to violate our Usage Policy"`, `r"Output blocked by content filtering policy"`, both **route**.

### Providers with no error examples at all

Reported honestly, since absence is a finding:

- **OpenAI / Codex.** All 6 `codex-headless` runs exited 0; each `stream.log` contains a single `{"type":"thread.started"}` line and each `final.txt` reads `finished`. These are smoke tests. **There is zero OpenAI provider error text in this corpus.** The one codex-routed conductor run that failed (`gpt-5.6-sol`, `failure_class: response-loss`) failed on a *conductor* `RuntimeError: source-bound recovery command identity changed`, not a provider error.
- **Muse** (6 runs) and **Prime Agent** (5 runs): all exit 0, trivial payloads (`contributor-ok`, `ZAI_OK`, `HEADLESS_OK`). No errors. Muse appears in the error corpus only as the *subject* of a `model_not_found`.
- **Moonshot Kimi**: exactly one provider error (§12). The other non-zero exits were 125/supervisor-loss and one 130 (SIGINT). Kimi's `stream.jsonl` is plain OpenAI-style `{role, content, tool_calls}` with **no result or terminal event type whatsoever** — 1,861 `tool` and 1,563 `assistant` records, plus `meta` records, and nothing else. Kimi therefore offers the detector no structured terminal signal at all; only exit code, stderr tail, and the pane.

---

## Structured data vs. what appears on screen

This is the part that matters most, and where I am most exposed. The detector reads a **rendered pane**. Almost everything above was extracted from **structured JSONL**. Here is exactly what I observed versus what I am inferring.

### Directly observed in rendered form (high confidence)

Only these, and only because a headless `final.txt` is literally the harness's rendered final output:

1. `API Error: Connection closed mid-response. The response above may be incomplete.` — observed as the **last line** of `deepseek-claude-headless/20260807-013127-49004-14326/final.txt`, appended to completed prose ("…Let me now check the remaining call sites…"). This is the exact on-screen shape the detector targets: **partial work, then the error line, then nothing.**
2. `Failed to authenticate. API Error: 401 Authentication Fails, Your api key: <redacted> is invalid` — the entire contents of `claude-headless/20260806-153103-82781-31069/final.txt`.
3. `Execution error` — entire contents of three `final.txt` files.
4. The Kimi OAuth line and the supervisor-vanished line — observed in `stderr.log`, which is a real terminal stream, though stderr and the TUI pane are not the same surface.

### Inferred, not observed

For everything else I am reading a JSON string field and **assuming** the harness prints it verbatim. Specific risks:

- **The `·` separator.** Strings like `Login expired · Please run /login` and `API Error: Request rejected (429) · [1308][…]` use U+00B7. I never saw one rendered. If the TUI splits on it, wraps, or renders it as a styled divider, a regex requiring `·` fails. **Every regex above avoids matching on `·`.**
- **Line wrapping.** The 500/529 strings are 150–230 characters. In an 80- or 100-column pane they *will* wrap, and a pane-capture regex spanning the whole sentence will not match. This is why I anchored the 5xx regexes on the short leading fragment (`API Error:\s*500 Internal server error`) rather than the full sentence — the trailing advice ("check https://status.claude.com") may land on a different line.
- **The em-dash** in `usually temporary — try again in a moment` is U+2014. Avoid it in patterns.
- **Terminal decoration.** The conductor `scrollback.txt` captures show the real rendering is prefixed and bulleted (`[tool] Bash — pending`, `💬`, `🔧`, `•`). An error line may well carry a leading glyph or ANSI colour. **All regexes above are unanchored at the start** (no `\A`, no `^`) precisely so a prefix cannot break them — except `Execution error`, which I anchored deliberately because the bare phrase is too generic to match loosely.
- **Truncation.** `scrollback.txt` files are ~8KB; a pane capture is a fixed number of rows. If the error is followed by a spinner or status line, it may scroll out of a short capture window.

### The trap: structured error ≠ stopped agent

Three cases where the JSONL says "error" and the agent did **not** stop, all of which would cause a false nudge:

- The 64 `system/api_error` retry events (§5) — 13 ECONNRESET recovered silently, and the harness has `maxRetries: 10`.
- The `model_refusal_fallback` (§9) — `level: "warning"`, harness auto-switched model and continued.
- The auto-mode classifier denials (§10) — `is_error: true` on a tool result, agent explicitly told to keep working.

And the inverse — the agent stopped and there is **no error string at all**: `stop_reason: "max_tokens"` (§14, 11 occurrences). A screen-reading detector cannot see this.

### One more asymmetry worth flagging

In the headless stream, the run that failed with a 401 emitted `"subtype": "success"` **alongside** `"terminal_reason": "api_error"`, `"api_error_status": 401`, and `"is_error": true`. So `subtype` is not a reliable success signal — `terminal_reason` and `is_error` are. And the connection-closed run wrote **no stream file at all**: the error existed only in `final.txt`. A detector that trusts structured output alone would have missed the single most important case in this entire corpus.

---

## Suggested detector ordering

Match in this order; first hit wins. Rationale: the specific quota strings must be tested before the generic `API Error:` prefix, or a 429 gets nudged.

```python
RULES = [
    (r"denied by the Claude Code auto mode classifier",              "ignore"),
    (r"safeguards flagged this message.*?Switched to",               "ignore"),
    (r"Agent terminated early due to an API error:\s*(?P<inner>.+)", "unwrap"),
    (r"Credit balance is too low",                                   "escalate"),
    (r"Failed to authenticate\. API Error:\s*401",                   "escalate"),
    (r"API Error:\s*401 token expired or incorrect",                 "escalate"),
    (r"Login expired",                                               "escalate"),
    (r"There's an issue with the selected model \((?P<model>[^)]+)\)","route"),
    (r"You've reached your .+ limit\. Run /usage-credits",           "route"),
    (r"appears to violate our Usage Policy",                         "route"),      # unverified locally
    (r"Output blocked by content filtering policy",                  "route"),      # unverified locally
    (r"You've hit your weekly limit",                                "wait"),
    (r"Usage limit reached for \d+\s*hour",                          "wait"),
    (r"\[1302\]\[Rate limit reached for requests\]",                 "nudge"),
    (r"API Error:\s*Connection closed mid-response",                 "nudge"),
    (r"API Error:\s*Server error mid-response",                      "nudge"),
    (r"API Error:\s*5\d\d\b",                                        "nudge"),
    (r"service may be temporarily overloaded",                       "nudge"),
    (r"Unable to connect to API \([A-Z]+\)",                         "nudge"),
    (r"error: failed to run prompt:",                                "relaunch"),   # process is dead
    (r"supervisor vanished without an exit sentinel",                "escalate"),
]
```

Two operational notes. First, `nudge` should be gated on the pane being **idle** — the same ECONNRESET text appears during successful auto-retry, and nudging mid-retry corrupts the turn. Second, `Execution error` is deliberately absent: it is unclassified, and matching two such common words risks firing on ordinary agent prose.

---

## Security

No credential, token, key, cookie or authorization header is reproduced anywhere above. Three redactions were applied: Z.ai request-id hashes in the 429/529 strings; the masked API-key suffix in the 401 string; and the auto-mode classifier rationale, one instance of which quotes a command line containing a real GitHub PAT. Partially-masked GitHub PATs were also encountered in `kimi-headless/20260806-210546-5558-554/stderr.log` (`gh auth status` output) and are deliberately not quoted. Nothing under `~/.secrets/` was read.
