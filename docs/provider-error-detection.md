# Provider error detection

Reference for the `errored-recoverable` screen detector (layer 0 of the recovery
ladder). Built from three independent passes over real evidence, not from
provider API documentation — an API returns `overloaded_error`, but the pane
shows whatever the CLI chose to render, and only the second is visible to a
detector reading a screen.

Status: research complete, detector not implemented.

---

## 1. Evidence tiers

Every pattern carries one of three provenances. They are not interchangeable.

| tier | meaning | trust |
|---|---|---|
| **binary** | read out of the shipped executable or bundle on this machine | tells you what the CLI *can* print |
| **observed** | seen in a real captured pane, transcript, or rendered `final.txt` | tells you what it *did* print |
| **documented** | the vendor states this is the rendered text | good, but every client re-wraps |

Moonshot says this better than we could, about its own errors:

> "If you are using a third-party client such as OpenCode or Claude Code, the
> client may transform or re-wrap error codes… **focus on the text content of
> the error message**."

The strongest patterns are the ones where a binary literal and a real capture
agree. `API Error: Connection closed mid-response` is the only string confirmed
in all four forms — bundle literal, structured transcript event, rendered
`final.txt`, and a live TUI pane.

---

## 2. Actions

The detector does not emit "there is an error". It emits what would clear the
screen, because those are genuinely different operations:

| action | meaning |
|---|---|
| `nudge` | send text; the turn resumes |
| `select` | a blocking menu is up; send a specific option number, then Enter |
| `wait` | a stated reset exists; nudging before it burns another rejection |
| `route` | needs a model or provider change; text will not clear it |
| `relaunch` | the process is dead; there is no pane to nudge |
| `escalate` | a human must act (auth, billing, config) |
| `ignore` | recognised, and NOT a stop — do not act |

`select` exists because of Codex. Its `Additional safety checks` overlay offers
`1. Retry with a faster model / 2. Keep waiting / 3. Learn more`, and sending
"continue" does nothing. **Option 1 silently downgrades the model**, so a
detector that blindly presses Enter on a default picks a routing change nobody
decided.

---

## 3. The `ignore` set — the rules that prevent harm

These matter more than the recovery patterns. A false nudge is a write into a
live composer, which is what the pane-input arbiter exists to prevent.

**A retry ladder is not a stall.** Every CLI examined self-heals before it gives
up, and says so on screen:

| provider | on-screen | rule |
|---|---|---|
| Claude Code | `✻ API error · Retrying in 1s · attempt 1/10` | suppress while `n < max` |
| Claude Code | `Waiting for API response · will retry in … · check your network` | fires after 20s of silence; the request has **not** failed |
| Codex | `Reconnecting... 2/5`, `stream disconnected - retrying sampling request` | suppress while retrying |
| Kimi | `Provider rate limit; subagent requeued for retry.` | self-healing |
| Muse | `attempt 3 retrying in 4s` | self-healing |

Claude Code's own shipped agent-status classifier states the rule outright:
*"Agent hit an error but is retrying or investigating → 'working'."*

**A structured error is not a stopped agent.** In Claude Code transcripts,
`system/api_error` records are internal retries that never render (64 observed,
13 `ECONNRESET` all recovered silently); only `isApiErrorMessage: true` reaches
the screen. A detector reading logs rather than the pane fires on the wrong one.

**Supervisor prose quotes error strings verbatim.** Agents write post-mortems
containing these exact sentences. Every pattern must be line-anchored with the
CLI's glyph prefix (`⏺ ⚠ ■ ⓘ ⎿ ✻ • └`) or the detector reads an agent *talking
about* an error and declares a healthy pane stuck.

**An already-completed auto-downgrade is not a stall.** `Switched to Opus 4.8`,
`routed to gpt-5.2 as a fallback`, `Your session model is unchanged` — the turn
produced an answer. Do not nudge. **Do** flag it: the answer came from a weaker
model, which the supervisor needs when judging the result.

---

## 4. Patterns by provider

Highest-confidence entries only. Full tables with occurrence counts, evidence
paths and lower-confidence rows are in the research reports.

### Claude Code (2.1.224)

| pattern | action | tier |
|---|---|---|
| `API Error: Connection closed mid-response` | nudge | binary + observed |
| `API Error: Server error mid-response` | nudge | binary + observed |
| `API Error: Response stalled mid-stream` | nudge | binary |
| `API Error: Connection closed while thinking` | nudge | binary |
| `Repeated 529 Overloaded errors` | nudge | binary |
| `This is a server-side issue, usually temporary` | nudge | binary + observed |
| `Server is temporarily limiting requests \(not your usage limit\)` | wait | binary |
| `API Error: Request rejected \(429\)` | wait | binary + observed |
| `You've hit your (session\|weekly\|Opus\|Fable 5) limit` | wait | binary + observed |
| `safeguards flagged this message` | route | binary + observed |
| `can't help with this\. Start a new session` | route | binary |
| `Prompt is too long` / `reached its context window limit` | route | binary |
| `Credit balance is too low` | escalate | binary + observed |
| `(Login expired\|Not logged in\|OAuth token revoked) · Please run /login` | escalate | binary + observed |
| `· Retrying in .+ · attempt \d+/\d+` | **ignore** | binary + observed |

### Codex (0.146.1)

| pattern | action | tier |
|---|---|---|
| `Additional safety checks` + `Retry with a faster model` | **select `2`** | observed |
| `This content can't be shown` + `extra caution with cybersecurity requests` | route | observed |
| `Selected model is at capacity\. Please try a different model` | route | binary + observed |
| `You've hit your usage limit for .+\. Switch to another model now` | route | binary |
| `Approaching rate limits` + `Switch to .+ for lower credit usage\?` | **select** | observed |
| `flagged for potentially high-risk cyber activity` | nudge + flag | binary |
| `stream disconnected before completion` | nudge | binary + observed |
| `SSE stream disconnected in the middle of a turn` | nudge | binary |
| `We're currently experiencing high demand` | nudge | binary |
| `Conversation interrupted - tell the model what to do differently` | nudge | observed |
| `ran out of room in the model's context window` | route | binary |
| `Quota exceeded\. Check your plan and billing details` | escalate | binary |
| `Reconnecting\.\.\. \d+/\d+` | **ignore** | observed |

Codex also ships a wire-level code enum (`cyber_policy`, `usage_limit_exceeded`,
`server_overloaded`, `context_window_exceeded`, …). **Anchoring on those in JSON
mode is more reliable than prose matching** wherever the orchestrator can use it.

### Kimi Code (0.34.0)

Kimi ships `KIMI_ERROR_INFO`, a machine-readable registry where **the vendor
marks each code retryable or not**. Prefer it over any regex.

| pattern | action | vendor `retryable` |
|---|---|---|
| `error: Provider connection error` | nudge | true |
| `error: Provider rate limit` | wait | true |
| `error: Provider filtered response` | route | false |
| `error: Provider API error` | escalate | false |
| `error: Provider authentication error` | escalate | false |
| `error: Context window overflow` | route | true |

Note the vendor's own distinction: a quota-exhausted 429 is deliberately
re-mapped away from `rate_limit`, with the shipped comment that requeueing
"cannot help until the account is recharged." That is `wait` versus `escalate`,
decided by the vendor.

### OpenCode (1.17.8)

| pattern | action |
|---|---|
| `Provider is overloaded` | nudge |
| `Rate Limited` / `Too Many Requests` | wait |
| `usage limit reached\. It will reset in ` | wait |
| `blocked by the provider's content filter` | route |
| `Input exceeds context window of this model` | route |

OpenCode ships a **19-pattern cross-provider context-overflow regex array** —
the single most reusable artifact found. Take it wholesale rather than
rebuilding it.

### Muse (0.1.0-R708.1)

Thinnest coverage; strings are Rust error fragments and the concatenated
on-screen form was inferred, not read from a format call.

| pattern | action |
|---|---|
| `model stream ended before terminal event` | nudge |
| `is temporarily unavailable while a problem with it is fixed` | wait |
| `authentication failed: ` | escalate |

No safety-classifier refusal string exists in the binary.

---

## 5. Two findings that change the design

### The same block behaves differently by configuration

> "With automatic model switching off, a request that falls back **pauses the
> conversation** instead of switching models."
> — support.claude.com/en/articles/16049681

That paused conversation *is* the silent stall this detector exists to catch,
and whether it occurs is a client setting rather than provider behaviour. The
detector cannot infer recoverability from the error class alone. Where a setting
decides it, read the setting.

### A stall with no error text at all

11 transcript messages ended on `stop_reason: "max_tokens"` — a genuine mid-work
stop that "continue" resolves, producing **zero** error output. A screen-reading
detector is structurally blind to it. Catch it from the structured field where
one is available; otherwise it is layer 1's idle timeout that finds it, which is
one of the reasons layer 1 cannot be replaced by layer 0.

---

## 6. Coverage gaps, stated rather than filled

- **No OpenAI error data in the captured corpus.** All six `codex-headless` runs
  were smoke tests that exited 0. Codex patterns above come from the binary and
  from one supervisor log.
- **Kimi produced no provider-level failure string** across ~2,700 rendered
  segments, and emits no structured terminal event type at all — only exit code,
  stderr, and the pane.
- **`muse_cli` and `opencode_cli` have zero terminal logs.** Every GLM hit was a
  supervisor writing prose about routing to them.
- **Claude Code has no quota banner in the corpus**, so no rate-limit regex was
  derived from observation for it. The binary supplies one; it is untested here.
- **Nothing was executed.** No CLI was driven to force these paths, so no
  binary-tier string has been seen on screen by this research. Presence in a
  bundle is not proof of reachability: several are behind settings or server
  flags.

---

## 7. Version drift

These are five specific builds — Claude Code 2.1.224, Codex 0.146.1, Kimi 0.34.0,
OpenCode 1.17.8, Muse 0.1.0-R708.1. Claude Code alone shipped five versions in
two weeks on this machine.

Anything keyed to exact prose will rot. The durable anchors are structural: the
`API Error:` prefix, Codex's `cyber_policy` code, Kimi's `error: ${title}` shape
and its `retryable` field, OpenCode's short constant strings. Prefer the short
distinctive head of a literal over a whole sentence, which also survives line
wrapping in a narrow pane.

Do not hard-code model names. They appear inside the interstitial text
(`gpt-5.4-mini`, `Opus 4.8`) and change; capture them instead.

Unicode: `·` is U+00B7 and `—` is U+2014. Both appear in real messages and both
are avoidable in patterns. No pattern here requires either.
