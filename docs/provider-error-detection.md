# Provider error detection

Reference for the `errored-recoverable` screen detector (layer 0 of the recovery
ladder). Built from three independent passes over real evidence, not from
provider API documentation — an API returns `overloaded_error`, but the pane
shows whatever the CLI chose to render, and only the second is visible to a
detector reading a screen.

Status: the narrow M6a initial contract is implemented dark. Static provider-
name dispatch recognizes the Claude mid-response terminal line as
`terminal`/`nudge`, the observed retry banner as `self-retrying`/`ignore`, and an
anchored generic Claude API error as `unknown`/`layer-2`. The other patterns
below remain research candidates and have no executing action until a targeted
local evidence gate admits them.

The mid-response line is matched in two wordings, and **they do not share
provenance.** `Connection closed` was captured on a live pane here.
`Connection lost` is the 2.1.233 replacement, read out of the bundle and never
seen to render — it is carried because the observed wording is absent from every
current build, so matching only the proven one would recognise nothing. Each
match records which of the two it was, so a reader of the journal can tell an
observed detection from a bundle-derived one rather than inheriting the older
row's evidence.

Each active match is published as one nested, bounded
`recovery_evidence` object. Its occurrence id is durable per exact terminal
generation across polling and daemon restart. A clear or different match closes
that occurrence; a later recurrence in the same generation receives a new id.
This journal is observation/dedupe evidence only: it sends no input, wakes no
supervisor, and has no completion or task-effect authority.

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
agree. No string currently holds all four forms — bundle literal, structured
transcript event, rendered `final.txt`, and a live TUI pane. The one that did,
`API Error: Connection closed mid-response`, is absent from Claude Code 2.1.233;
the wording is now `Connection lost mid-response`. A four-form confirmation dates
a build rather than outliving one, so treat tier as a statement about the version
in the heading.

Counts below are reproducible with `strings <binary> | grep -cF '<literal>'`
against the shipped executables, not the launcher shims — Codex's is the native
`codex-darwin-arm64` vendor binary and Claude Code's is `claude.exe` in the npm
package. Running that grep against `/opt/homebrew/bin/codex`, which is a Node
wrapper, returns zero for every string and reads as a corpus-wide refutation.

**A count is only ever the literal in its own row.** Where a pattern is a regex,
or carries a character the bundle does not store as bytes, the count column names
the plain substring that was actually counted. Do not read a count as
verification of the regex beside it.

The character that catches this is `·` (U+00B7), which appears in many rendered
Claude Code lines. The bundle contains **zero** U+00B7 bytes and 267 instances of
the source escape `\xB7`, so `grep -cF '· Please run /login'` returns 0 while the
line renders normally. Count the fragment on one side of the separator, or search
for the escape form; never conclude from a zero that a `·` line does not exist.

Two composition rules explain most zero counts on strings that plainly render.
Codex is Rust and stores format templates with `{}` holes, so one rendered line
is often several pooled literals. Claude Code is a Bun bundle that holds `API
Error` as a separate constant from every message body, so a full-sentence literal
like `API Error: Connection refused` counts zero while the line still appears on
screen. **Anchor on the distinctive body fragment, never on a whole rendered
sentence.**

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

`select` exists because of Codex. Its safety-buffering dialog —
`Our systems are thinking a bit more about this request before responding.` with
`Retry with a faster model` and `Dismiss and keep waiting` — does not clear on
"continue". **The faster-model option silently downgrades the model**, so a
detector that blindly presses Enter on a default picks a routing change nobody
decided. The dialog self-dismisses when the response arrives, which makes
`ignore` defensible; where it is acted on, choose the dismiss-and-wait option.
The same rule governs `Approaching rate limits`, whose options are
`Keep current model` and `Keep current model (never show again)`: never accept a
switch on the agent's behalf.

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
| Claude Code | `[Usage limit reached — grace window active. Wrap up…]` | a system-reminder to the agent, not a halt |
| Codex | `Reconnecting... 2/5`, `stream disconnected - retrying sampling request` | suppress while retrying |
| Codex | `Previous response was not found. Retrying the full request.` | self-healing |
| Codex | `Responses may take longer because extra safety checks are on.` | a delay notice, not a stop |
| Kimi | `Provider rate limit; subagent requeued for retry.` | self-healing |
| Muse | `attempt 3 retrying in 4s` | self-healing |

Claude Code's own shipped agent-status classifier states the rule outright:
*"Agent hit an error but is retrying or investigating → 'working'."*

**The last attempt is not the same as the ones before it.** `attempt 10/10` is
the final banner before the terminal `⏺ API Error:` line, so a detector that
parses `n/max` gets a free lookahead: `n < max` is working, `n == max` is about
to stop. That is worth carrying because it is the one point where layer 0 can
anticipate a halt instead of waiting to observe it. It does not license nudging
at `n == max` — the turn is still live, and the terminal line is what admits a
recovery action.

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

### Claude Code (2.1.233)

Counts are `CL`; every row below is `CX` 0 unless the exclusivity notes say
otherwise. Patterns are body fragments, for the composition reason in §1.

| pattern | action | tier | CL |
|---|---|---|---|
| `Connection lost mid-response` | nudge | binary | 2 |
| `Server error mid-response` | nudge | binary | 2 |
| `The response stopped arriving` | nudge | binary | 2 |
| `The response stalled before a response was produced` | nudge | binary | 2 |
| `went to sleep (mid-response\|before a response was produced)` | nudge | binary | 2 |
| `firewall or proxy may be blocking` | nudge | binary + observed | 1 |
| `Can't reach the API server` | nudge | binary | 1 |
| `Repeated 529 Overloaded errors` | nudge | binary + observed | 2 |
| `This is a server-side issue, usually temporary` | nudge | binary + observed | 1 |
| `Request was aborted` | nudge | binary | 5 |
| `due to tool use concurrency issues` / `duplicate tool_use ID in conversation history` | nudge | binary | 2 / 2 |
| `Server is temporarily limiting requests \(not your usage limit\)` | wait | binary | 2 |
| `Request rejected \(429\)` | wait | binary + observed | 2 |
| `(session\|weekly\|Opus\|Sonnet\|Fable 5\|usage credit) limit` | wait | binary | 6/3/3/2/6/10 |
| `You've hit your .+ · resets ` | wait | binary + observed | 13 (`You've hit your `) |
| `is experiencing high load, please use /model` | route | binary | 3 |
| `Prompt is too long` / `reached its context window limit` | route | binary + observed | 3 / 2 |
| `safeguards flagged this message` | route | binary + observed | 8 |
| `can't help with this\. Start a new session` | route | binary | 2 |
| `Claude ended this conversation` | route | binary | 14 |
| `monthly spend limit` | escalate | binary | 17 |
| `Credit balance is too low` | escalate | binary + observed | 2 |
| `· Please run /login` | escalate | binary | 25 (`Please run /login`) |
| `· Retrying in .+ · attempt \d+/\d+` | **ignore** | binary + observed | 2 (`Retrying in `) |
| `Waiting for API response` | **ignore** | binary | 2 |
| `grace window active` | **ignore** | binary | 1 |

`monthly spend limit` is `escalate` rather than `wait` because a monthly reset is
too far out to hold a worker against; it needs a human or a model change.

The suffixes ` (rate-limited)`, ` (overloaded)`, ` (server error)`,
` (timed out)`, ` (connection failed)`, ` (request timed out)` are appended to
messages rather than standing alone. Use them to sharpen a line already matched,
not as rows.

### Codex (0.147.0)

Counts are `CX`; every row is `CL` 0 unless noted. Trailing spaces and colons are
significant — they mark where a Rust format hole follows.

| pattern | action | tier | CX |
|---|---|---|---|
| `exceeded retry limit, last status: ` | nudge | binary + observed | 1 |
| `stream disconnected before completion: ` | nudge | binary + observed | 1 |
| `Error while reading the server response: ` | nudge | binary | 1 |
| `Connection failed: ` | nudge | binary | 1 |
| `unexpected status ` | nudge | binary + observed | 2 |
| `We're currently experiencing high demand` | nudge | binary + observed | 1 |
| `Codex is currently experiencing high load.` | nudge | binary | 1 |
| `Conversation interrupted - tell the model what to do differently` | nudge | binary + observed | 1 |
| `turn aborted. Something went wrong?` | nudge | binary | 1 |
| `flagged for potentially high-risk cyber activity` | nudge + flag | binary | 5 |
| `internal error; agent loop died unexpectedly` | nudge | binary | 1 |
| `session configured event was not the first event in the stream` | relaunch | binary | 1 |
| `Error running remote compact task` | *re-classify* | binary + observed | 7 |
| `Selected model is at capacity. Please try a different model.` | route | binary + observed | 1 |
| `You've hit your usage limit for ` + `. Switch to another model now,` | route | binary | 1 |
| `ran out of room in the model's context window` | route | binary + observed | 1 |
| `This content can't be shown` + `extra caution with cybersecurity requests` | route | binary + observed | 1 |
| `You've hit your usage limit` | wait | binary + observed | 7 |
| `Usage limit reached` | wait | binary | 2 |
| `Quota exceeded. Check your plan and billing details.` | escalate | binary + observed | 1 |
| `You're out of credits. ` / `You've reached your workspace credit limit` | escalate | binary | 1 / 1 |
| `Your access token could not be refreshed. ` | escalate | binary | 22 |
| `Our systems are thinking a bit more about this request` | **select** / ignore | binary | 1 |
| `Approaching rate limits` | **select** | binary | 1 |
| `Heads up, you have less than` | **ignore** | binary | 1 |
| `Reconnecting\.\.\. ` | **ignore** | binary | 1 |
| `Falling back from WebSockets to HTTPS transport` | **ignore** | binary + observed | 7 |
| `request timed out`, after that fallback line | nudge | binary + observed | 11 |
| `Goal budget reached - the turn was stopped.` | **ignore** | binary | 1 |

`exceeded retry limit, last status: ` is the explicit retries-exhausted marker
and the highest-value row in this table: it is the one string that states the CLI
already tried and gave up, interpolating the final status. A worker that halts
behind it has exhausted recovery the provider could do for itself, which is
precisely the case a nudge is for.

`Error running remote compact task` is a wrapper, not a classification — the real
error follows it inline. Match it, then re-classify on the inner string, which is
usually `stream disconnected…`, `You've hit your usage limit`, or
`Selected model is at capacity`.

`Goal budget reached - the turn was stopped.` is a deliberate stop the operator
or the config asked for. It renders like a failure and is not one.

`session configured event was not the first event in the stream` is `relaunch`
rather than `nudge` because it is a protocol violation: session state is already
suspect, so there is nothing sound to resume into.

The `Falling back from WebSockets` and `request timed out` rows are one sequence,
and reading them separately gets the action backwards. The fallback line **is**
Codex retrying — it switches transport in response to a timeout, so on its own it
means recovery is in progress and the correct action is none. A bare
`request timed out` following it means that retry also failed. Confirmed terminal
by observation: the turn stayed dead and did not resume.

That pairing is worth more than either string. It lets a detector separate
self-retry from terminal off the screen alone, without timing heuristics, for a
provider whose failures otherwise look identical at both stages.

**Neither transport string is provider-exclusive, and the qualifier is what
makes each row safe.** Counted in the shipped binaries: `request timed out`
appears 11 times in Codex and 18 times in Claude Code; `ConnectionRefused`
appears 15 and 18. Anchoring on either bare string would match the wrong
provider. What disambiguates is the accompanying text, and only because those
are exclusive: `Falling back from WebSockets` appears 7 times in Codex and never
in Claude Code, and `API Error` appears 13 times in Claude Code and never in
Codex. Check exclusivity by count in both binaries before adding a row, rather
than assuming a string belongs to the provider it was first seen on.

That check keeps catching things. Measured across these two builds, none of the
following is safe bare:

| string | CX | CL | safe form |
|---|---|---|---|
| `request timed out` | 11 | 18 | pair with the fallback line; Codex renders it lowercase, Claude Code capitalises `Request timed out` (0/16) |
| `ConnectionRefused` | 15 | 18 | Claude Code's rendered line is `firewall or proxy may be blocking` (0/1) |
| `context_window_exceeded` | 8 | 13 | needs a second Codex-only field |
| `You've hit your ` | 7 | 13 | keep the limit-name suffix |
| `Not logged in` | 3 | 10 | qualify with `Please run /login` (0/25) |
| `Usage limit reached` | 2 | 1 | qualify with a Codex suffix |
| `API error` (lowercase) | 1 | 50 | not usable at any casing |
| `ECONNRESET` | 2 | 76 | Bun internals dominate; no clean anchor |

Case is a free qualifier and worth spending before inventing a harder one.

Both rows were confirmed against Codex 0.147.0 and the Claude `Connection
refused` row against Claude Code 2.1.233, rather than the builds these tables
were first derived from. The strings are present in each shipped binary — 7 and
11 occurrences respectively in the Codex native binary, and `firewall or proxy
may be blocking` once with `ConnectionRefused` 18 times in the Claude binary —
and each was also seen on a live pane.

Codex also ships a wire-level code enum. **Anchoring on those in JSON mode is
more reliable than prose matching** wherever the orchestrator can use it, because
codes do not re-word between patch releases the way rendered sentences do.

| code | CX | CL |
|---|---|---|
| `usage_limit_exceeded` | 7 | 0 |
| `server_overloaded` | 8 | 0 |
| `cyber_policy` | 8 | 0 |
| `response_stream_disconnected` | 7 | 0 |
| `context_window_exceeded` | 8 | **13** |

**`context_window_exceeded` is the exception and must be carved out.** It appears
in both binaries, so it identifies the condition but not the provider. Pair it
with a second Codex-only field before treating a match as Codex, or the JSON path
inherits exactly the cross-provider collision the prose path was avoiding. The
other four codes are exclusive and safe alone.

### Kimi Code (0.36.1)

Kimi ships `KIMI_ERROR_INFO`, a machine-readable registry of
`{title, retryable, public, action}` per code, where **the vendor marks each code
retryable or not**. Prefer it over any regex.

| pattern | action | vendor `retryable` | K |
|---|---|---|---|
| `Provider connection error` | nudge | true | 1 |
| `Provider rate limit` | wait | true | 4 |
| `Provider overloaded` | wait | true | 1 |
| `Provider filtered response` | route | false | 2 |
| `Provider API error` | escalate | false | 1 |
| `Provider authentication error` | escalate | false | 1 |
| `Context window overflow` | route | true | 1 |

Counts are the bare titles. The rendered line is `error: ${title}`, composed at
runtime by `formatStartupError`, so the joined form `error: Provider rate limit`
is not a shipped literal and counts 0 — the same composition trap as Codex's
format holes and Claude Code's `API Error` constant. All three providers hit it,
which is why §1 says to anchor on the distinctive fragment rather than the line.

`formatStartupError` is the only renderer of `title`, and it covers the
startup/migration/upgrade path. **A mid-run provider failure does not surface
through these rows at all** — it goes through the wire protocol, where the
`turn.step.retrying` event carries `failedAttempt`, `nextAttempt`, `maxAttempts`
and `statusCode`. That event is the best retry signal any of these providers
ships: it gives the attempt counter as data instead of as text to be scraped, and
retries are exhausted when the turn ends in an error payload with
`retryable: false`. **Prefer it over every Kimi row above.**

Kimi carries a second registry, `ProtocolErrors` in `agent-core-v2`, which
re-declares the provider domain with different titles for the same conditions —
`Provider authentication failed` rather than `...error`, `Context overflow`
rather than `Context window overflow`. Match either, and do not assume one title
per condition.

`error; no more retries left` (K 9) and `error; not retryable` (K 3) look like
ideal exhaustion anchors and are **not exclusive** — both appear in Claude Code
(4 and 2), because the two bundle the same upstream SDK retry loops. Use them
only behind a Kimi-exclusive qualifier such as a `provider.*` code.

Note the vendor's own distinction: a quota-exhausted 429 is deliberately
re-mapped away from `rate_limit` to `api_error` with `retryable: false`, and the
transport retry predicate refuses it, with the shipped comment that the
rate-limit code "would re-mint a rate-limit error across the wire boundary and
drive the swarm requeue/suspend loop, which cannot help until the account is
recharged." That is `wait` versus `escalate`, decided by the vendor — and it is
the clearest statement any of these vendors makes that a retryable-looking status
can be a terminal condition.

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

## 5. Three findings that change the design

### A halt can exit 0

Claude Code has printed `API Error: Rate limit reached` to stdout with **exit
code 0**, and reported `"subtype":"success","is_error":true` in JSON mode
(anthropics/claude-code#79500). The two fields disagree inside one payload, and
the exit code is the one that lies.

This is the finding with the most direct claim on CAO, because CAO runs workers
headless. Any supervision keyed to exit status treats that run as a completed
task, and no screen-reading detector helps — there is no pane. Where a headless
result is used, read `is_error` rather than the exit code, and treat a `success`
subtype carrying `is_error: true` as the error.


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

- **Almost no OpenAI error data in the captured corpus.** All six
  `codex-headless` runs were smoke tests that exited 0, so most Codex patterns
  above come from the binary and from one supervisor log. The two transport rows
  are the exception: captured twice from a live pane, two minutes apart and
  byte-identical, with the outcome confirmed.
- **The transport failures here were caused by the shared network path, not by
  either provider.** All provider traffic on this installation routes through one
  residential proxy, so a single upstream fault surfaces as two different
  provider-shaped errors. The Codex and Claude captures above are from the same
  incident window: Codex at 09:57:40 and 09:58:37, Claude Code at 09:59:43. That
  simultaneity across providers is the only reliable tell, and it is computable
  from evidence the detector already emits — a per-provider classifier is not.
  Claude Code's own text declines to blame Anthropic (`a firewall or proxy may be
  blocking it`), so recording the line verbatim attributes nothing.
- **Kimi produced no provider-level failure string** across ~2,700 rendered
  segments. It does emit structured events — `turn.step.retrying` carries the
  attempt counters and the terminal payload carries `retryable` — so the earlier
  reading that only exit code, stderr and the pane were available was wrong. What
  is still missing is an *observation*: no Kimi failure has been captured on a
  pane or in a transcript here, so every Kimi row is binary-tier.
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

These are five specific builds — Claude Code 2.1.233, Codex 0.147.0, Kimi 0.34.0,
OpenCode 1.17.8, Muse 0.1.0-R708.1. Claude Code alone shipped five versions in
two weeks on this machine.

Anything keyed to exact prose will rot, and the rate is now measured rather than
asserted. Re-counting the earlier tables against nine Claude Code patch releases
(2.1.224 → 2.1.233) and one Codex minor (0.146.1 → 0.147.0) retired these:

| build | string that no longer exists | what replaced it |
|---|---|---|
| Claude Code | `Connection closed mid-response` | `Connection lost mid-response` |
| Claude Code | `Response stalled mid-stream` | `The response stalled before a response was produced` |
| Claude Code | `Connection closed while thinking` | removed; absent from the whole package |
| Claude Code | `API Error: Connection refused` | `Connection refused — a firewall or proxy may be blocking it` |
| Codex | `Additional safety checks` | the safety-buffering dialog in §2 |
| Codex | `SSE stream disconnected in the middle of a turn` | `stream disconnected before completion: ` |
| Codex | `Re-connecting... n/5` | `Reconnecting... n/5` |

Nine patch releases retired four of sixteen Claude Code rows. A row is worth
roughly one minor version, so a detector that hard-codes this table needs the
re-count wired into upgrade, not into someone's memory. The `strings | grep -cF`
command in §1 is the whole procedure.

**Anchor on the distinctive head of a literal, never on end-of-string.** The
evidence for that rule here is a survivor rather than a casualty:
`Server error mid-response` still matches because the change only appended
`The response above may be incomplete.`, and a head anchor rode it out.

Be clear about what head-anchoring does not buy. None of the four retired rows
would have survived it — `Connection closed`→`Connection lost` and
`Response stalled mid-stream`→`The response stalled before a response was
produced` both reworded the head (`Response stalled` counts 0), one row was
removed outright, and `Connection refused` was replaced by a different formatter.
Head-anchoring buys resilience to the most *common* edit, not to a rewrite. Only
the re-count catches those, which is why it belongs in upgrade rather than in
judgement.

That rule governs corpus anchors, and the executing detector deliberately does
not follow it. `_CONNECTION_CLOSED` and `_CONNECTION_LOST` in
`provider_recovery_evidence.py` match the whole rendered sentence anchored to
end-of-line, because their action is `nudge` — a write into a live composer — and
§3's rule against firing on an agent *quoting* an error is worth more there than
resilience to a future suffix. The two rules point opposite ways because they
price different mistakes: a corpus row that stops matching costs a missed
detection, while an executing pattern that matches too much costs an interrupted
worker. Expect this pair to need re-measuring on Claude Code upgrades, and treat
the `strings` re-count as the thing that catches it.

The durable anchors are structural: the `API Error:` prefix, Codex's
`cyber_policy` code, Kimi's `error: ${title}` shape and its `retryable` field,
OpenCode's short constant strings. A short head also survives line wrapping in a
narrow pane.

The Codex retry banner's grammar dates a log where nothing else does:
`stream error: …retrying n/5 in Xms` up to 0.46, `Re-connecting... n/5` through
0.55, `Reconnecting... n/5` from 0.56. Only the last exists in 0.147.0, so the
older two match archived transcripts and never a live pane.

Do not hard-code model names. They appear inside the interstitial text
(`gpt-5.4-mini`, `Opus 4.8`) and change; capture them instead.

Unicode: `·` is U+00B7 and `—` is U+2014. Both appear in real rendered messages.

Several Claude Code rows above are written with `·` because that is what the pane
shows, but **no row needs it and every one is better without it.** The separator
is where a line is most likely to gain a segment between releases, and the bundle
does not store the character as bytes at all (§1), so a `·`-bearing pattern is
both the most brittle form and the one that cannot be verified by the documented
count. Match a fragment on one side of the separator instead: `Please run /login`
rather than `· Please run /login`.
