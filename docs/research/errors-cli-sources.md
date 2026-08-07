# Agent-CLI rendered error strings — extracted from installed binaries

Read-only survey, 2026-08-07. Nothing outside the scratchpad was written or modified.

**What this document is.** Every literal in Table 1 was read out of a shipped executable
or bundle on this machine. Where a literal is a template, the minified variable is
resolved to its constant and the resolution is shown. Table 2 is documentation, which is
a weaker kind of evidence: it tells you an error condition exists, not what the pane
shows.

---

## Installed CLIs (verified)

| CLI | Version | Artifact actually grepped | Form |
|---|---|---|---|
| Claude Code | 2.1.224 | `/Users/colin/.local/share/claude/versions/2.1.224` | Mach-O arm64, Bun-compiled; JS source embedded verbatim |
| OpenAI Codex | 0.146.1 | `/opt/homebrew/lib/node_modules/@openai/codex/node_modules/@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex` | Rust native binary (`/opt/homebrew/bin/codex` → `bin/codex.js` → this) |
| Moonshot Kimi Code | 0.34.0 | `/opt/homebrew/lib/node_modules/@moonshot-ai/kimi-code/dist/main.mjs` | Bundled but **not minified** — comments and identifiers intact |
| OpenCode | 1.17.8 | `/opt/homebrew/lib/node_modules/opencode-ai/node_modules/opencode-darwin-arm64/bin/opencode` | Bun-compiled binary, minified JS embedded |
| Meta Muse Code | 0.1.0-R708.1 | `/Users/colin/.local/bin/muse-bin-0.1.0-R708.1` | Rust native binary (launched by the bash launcher `/Users/colin/.local/bin/muse`) |

Intermediate `strings -a -n 6` dumps are in the scratchpad
(`claude-2.1.224.strings`, `codex-0.146.1.strings`, `opencode-1.17.8.strings`,
`muse-0.1.0-R708.1.strings`) if you want to re-verify any line.

---

## Table 1 — rendered strings (from CLI source)

`^\s*` prefixes are omitted throughout; assume the detector matches anywhere in a line.
All regexes are written for `re.search(..., re.I)` unless noted.

### Claude Code 2.1.224

Source file for every row: `/Users/colin/.local/share/claude/versions/2.1.224`

The prefix constant is `cE = "API Error"`. Templates below are shown with `${cE}`
already resolved.

| Verbatim literal | Proposed Python regex | Action | Confidence |
|---|---|---|---|
| `API Error: Connection closed mid-response. The response above may be incomplete.` | `r"API Error: Connection closed mid-response"` | nudge | high |
| `API Error: Server error mid-response. The response above may be incomplete.` | `r"API Error: Server error mid-response"` | nudge | high |
| `API Error: Response stalled mid-stream. The response above may be incomplete.` | `r"API Error: Response stalled mid-stream"` | nudge | high |
| `API Error: Connection closed while thinking, before producing a response. Try again.` | `r"API Error: Connection closed while thinking"` | nudge | high |
| `API Error: Response stalled while thinking, before producing a response. Try again.` | `r"API Error: Response stalled while thinking"` | nudge | high |
| `API Error: Connection to the API was lost (${code}). This is usually temporary — try again.` | `r"API Error: Connection to the API was lost \("` | nudge | high |
| `API Error: Repeated 529 Overloaded errors. The API is at capacity — this is usually temporary. Try again in a moment.` | `r"Repeated 529 Overloaded errors"` | nudge | high |
| `API Error: ${msg}. This is a server-side issue, usually temporary — try again in a moment.` (all HTTP ≥ 500) | `r"API Error: .*This is a server-side issue, usually temporary"` | nudge | high |
| `Opus is experiencing high load, please use /model to switch to Sonnet` | `r"Opus is experiencing high load, please use /model to switch to Sonnet"` | route | high |
| `Fable is experiencing high load, please use /model to switch to Sonnet` | `r"Fable is experiencing high load, please use /model to switch to Sonnet"` | route | high |
| `Switched to ${model} due to high demand for ${orig}` | `r"Switched to .+ due to high demand for "` | nudge (already auto-recovered) | high |
| `Switched to ${model} because ${orig} is not available` | `r"Switched to .+ because .+ is not available"` | nudge (already auto-recovered) | high |
| `Switched to ${model} because ${orig} returned an error that could not be retried` | `r"Switched to .+ returned an error that could not be retried"` | route | high |
| `API Error: Server is temporarily limiting requests (not your usage limit) · ${detail}` | `r"Server is temporarily limiting requests \(not your usage limit\)"` | wait | high |
| `API Error: Request rejected (429) · ${detail}` | `r"API Error: Request rejected \(429\)"` | wait | high |
| `${errText} · Retrying in ${dur} (${resetTime}) · attempt ${n}/${max}` | `r"·\s*Retrying in .+·\s*attempt \d+/\d+"` | nudge (self-healing; do not interrupt) | high |
| `${errText} · will retry in ${dur} · check your network` | `r"·\s*will retry in .+·\s*check your network"` | nudge | high |
| `Waiting for API response` | `r"Waiting for API response"` | nudge (spinner state, not a stop) | medium |
| `Request timed out` | `r"\bRequest timed out\b"` | nudge | high |
| `Request timed out. Check your internet connection and proxy settings` | `r"Request timed out\. Check your internet connection"` | nudge | high |
| `API Error: Request was aborted.` | `r"API Error: Request was aborted\."` | nudge | high |
| `Connection interrupted by system sleep` | `r"Connection interrupted by system sleep"` | nudge | high |
| `You've hit your ${session limit\|weekly limit\|Opus limit\|Sonnet limit\|Fable 5 limit\|usage credit limit}` | `r"You've hit your (session limit\|weekly limit\|Opus limit\|Sonnet limit\|Fable 5 limit\|usage credit limit)"` | wait | high |
| `Approaching ${limit} · resets ${when}` | `r"Approaching (session\|weekly\|Opus\|Sonnet\|usage credit) limit"` | wait (warning only) | high |
| `You've used ${n}% of your ${limit} · resets ${when}` | `r"You've used \d+% of your .+ · resets "` | wait (warning only) | high |
| `/upgrade to increase your usage limit.` | `r"/upgrade to increase your usage limit"` | escalate | high |
| `Switch models to keep working.` | `r"Switch models to keep working\."` | route | high |
| `try /model sonnet · ~2× runway` / `try /model opus · more runway` / `try /effort medium` | `r"try /(model \w+\|effort \w+) ·"` | route | high |
| `You're out of usage credits. /model to switch models.` | `r"You're out of usage credits\."` | route | high |
| `You've hit your monthly spend limit. /model to switch models.` | `r"You've hit your monthly spend limit\."` | escalate | high |
| `Credit balance is too low` | `r"Credit balance is too low"` | escalate | high |
| **`API Error: ${model}'s safeguards flagged this message (https://www.anthropic.com/legal/aup). Our intentionally broad safeguards allow us to deliver more capabilities faster, but can sometimes flag legitimate coding, cybersecurity, and biology tasks. Claude Code can't respond to this message with ${model}.`** | `r"safeguards flagged this message"` | **route** | high |
| `API Error: ${model}'s safeguards flagged this message. This sometimes happens with safe, normal conversations.` | `r"safeguards flagged this message\. This sometimes happens"` | route | high |
| `API Error: ${model} can't help with this. Start a new session to continue.` | `r"can't help with this\. Start a new session to continue"` | route | high |
| `Try rephrasing the request in a new session or change your model.` | `r"Try rephrasing the request in a new session or change your model"` | route | high |
| `Double press esc to edit your last message, or try a different model with /model.` | `r"try a different model with /model"` | route | high |
| `${model}'s safeguards flagged this message. ... Switched to ${fallback}. Send feedback with /feedback or learn more: ...` | `r"safeguards flagged this message\..*Switched to "` | nudge (auto-downgrade already happened) | high |
| `... This response was generated by ${model} instead. Your session model is unchanged.` | `r"This response was generated by .+ instead\. Your session model is unchanged"` | nudge | high |
| `... This response was completed by ${model}. Your session model is unchanged.` | `r"This response was completed by .+\. Your session model is unchanged"` | nudge | high |
| `automatically switched from ${model} after a message was flagged — run /model to switch back` | `r"automatically switched from .+ after a message was flagged"` | nudge | high |
| `Apply to the Cyber Verification Program to reduce these interruptions.` | `r"Apply to the Cyber Verification Program"` | route | high |
| `Prompt is too long` | `r"\bPrompt is too long\b"` | route | high |
| `API Error: The model has reached its context window limit.` | `r"The model has reached its context window limit"` | route | high |
| `API Error: Claude's response exceeded the ${n} output token maximum. To configure this behavior, set the CLAUDE_CODE_MAX_OUTPUT_TOKENS environment variable.` | `r"response exceeded the \d+ output token maximum"` | route | high |
| `... has reached its context window limit. Run /compact, or double press esc to go back and remove attachments.` | `r"Run /compact, or double press esc"` | route | high |
| `API Error: Usage credits required for 1M context · run /usage-credits to turn them on, or /model to switch to standard context` | `r"Usage credits required for 1M context"` | route | high |
| `API Error: 401 Invalid API key · Please run /login` | `r"API Error: 401 Invalid API key"` | escalate | high |
| `Not logged in · Please run /login` | `r"Not logged in · Please run /login"` | escalate | high |
| `Login expired · Please run /login` | `r"Login expired · Please run /login"` | escalate | high |
| `OAuth token revoked · Please run /login` | `r"OAuth token revoked · Please run /login"` | escalate | high |
| `OAuth refresh token is no longer valid; run /login to re-authenticate` | `r"OAuth refresh token is no longer valid"` | escalate | high |
| `Invalid API key · Fix external API key` | `r"Invalid API key · Fix external API key"` | escalate | high |
| `Authentication error · This may be a temporary network issue, please try again` | `r"Authentication error · This may be a temporary network issue"` | nudge | high |
| `Your organization has disabled API key authentication · ...` | `r"Your organization has disabled API key authentication"` | escalate | high |
| `Your ANTHROPIC_API_KEY belongs to a disabled organization · ...` | `r"belongs to a disabled organization"` | escalate | high |
| `Your organization has disabled Claude subscription access for Claude Code · Use an Anthropic API key instead, or ask your admin to enable access` | `r"disabled Claude subscription access for Claude Code"` | escalate | high |
| `Your apiKeyHelper script is failing · This usually means you need to re-authenticate with your provider · Run /status to see the script's error output` | `r"Your apiKeyHelper script is failing"` | escalate | high |
| `AWS credentials expired or invalid` / `AWS authentication failed` | `r"AWS (credentials expired or invalid\|authentication failed)"` | escalate | high |
| `Google Cloud credentials expired or invalid` / `Google Cloud authentication failed` | `r"Google Cloud (credentials expired or invalid\|authentication failed)"` | escalate | high |
| `Unable to connect to API: SSL certificate verification failed. Check your proxy or corporate SSL certificates` (+ 6 sibling SSL messages) | `r"Unable to connect to API: SSL"` | escalate | high |
| `API Error: 400 due to tool use concurrency issues.` | `r"API Error: 400 due to tool use concurrency issues"` | nudge | high |
| `API Error: 400 duplicate tool_use ID in conversation history.` | `r"duplicate tool_use ID in conversation history"` | nudge | high |
| `The model ${m} is not available on your ${platform} deployment. Try /model to switch to ${alt}, or ask your admin to enable this model.` | `r"is not available on your .+ deployment"` | escalate | high |
| `There's an issue with the selected model (${m}). It may not exist or you may not have access to it.` | `r"There's an issue with the selected model \("` | escalate | high |
| `API Error: Please wait a moment and try again.` (bare-`API Error` display fallback) | `r"API Error: Please wait a moment and try again\."` | nudge | high |
| `Error streaming, falling back to non-streaming mode: ` | `r"Error streaming, falling back to non-streaming mode"` | nudge | high |
| `Streaming endpoint returned 404, falling back to non-streaming mode` | `r"Streaming endpoint returned 404, falling back"` | nudge | high |
| `Non-streaming fallback also failed: ` | `r"Non-streaming fallback also failed"` | unknown | medium |
| `<tool_use_error>Error: Streaming fallback - tool execution discarded</tool_use_error>` | `r"Streaming fallback - tool execution discarded"` | nudge | high |
| `Fast mode disabled · usage credits exhausted` (+5 sibling variants) | `r"Fast mode disabled ·"` | nudge (degraded, still working) | high |

### OpenAI Codex 0.146.1

Source file for every row:
`/opt/homebrew/lib/node_modules/@openai/codex/node_modules/@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex`

| Verbatim literal | Proposed Python regex | Action | Confidence |
|---|---|---|---|
| `We're currently experiencing high demand, which may cause temporary errors.` | `r"We're currently experiencing high demand"` | nudge | high |
| `Selected model is at capacity. Please try a different model.` | `r"Selected model is at capacity\. Please try a different model"` | route | high |
| `stream disconnected before completion: ` | `r"stream disconnected before completion"` | nudge | high |
| `The response SSE stream disconnected in the middle of a turn before completion.` | `r"SSE stream disconnected in the middle of a turn"` | nudge | high |
| `Failed to connect to the response SSE stream.` | `r"Failed to connect to the response SSE stream"` | nudge | high |
| `stream disconnected - retrying sampling request (` | `r"stream disconnected - retrying sampling request"` | nudge (self-healing) | high |
| `stream closed before response.completed` | `r"stream closed before response\.completed"` | nudge | high |
| `idle timeout waiting for SSE` | `r"idle timeout waiting for SSE"` | nudge | high |
| `SSE Error: ` | `r"SSE Error: "` | nudge | medium |
| `Reached the retry limit for responses.` | `r"Reached the retry limit for responses"` | nudge | high |
| `exceeded retry limit, last status: ` | `r"exceeded retry limit, last status:"` | nudge | high |
| `request timed out` | `r"\brequest timed out\b"` | nudge | high |
| `turn aborted. Something went wrong? Hit `/feedback` to report the issue.` | `r"turn aborted\. Something went wrong\?"` | nudge | high |
| `Error while reading the server response: ` | `r"Error while reading the server response"` | nudge | medium |
| `Connection failed: ` | `r"^Connection failed: "` | nudge | medium |
| `Codex ran out of room in the model's context window. Start a new thread or clear earlier history before retrying.` | `r"ran out of room in the model's context window"` | route | high |
| `shared rollout token budget exhausted` | `r"shared rollout token budget exhausted"` | route | high |
| `You've hit your usage limit for ${model}. Switch to another model now,` (+ ` or try again at ${time}`) | `r"You've hit your usage limit for .+\. Switch to another model now"` | **route** | high |
| `You've hit your usage limit. Upgrade to Plus to continue using Codex (https://chatgpt.com/explore/plus),` | `r"You've hit your usage limit\. Upgrade to Plus"` | escalate | high |
| `You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more credits` | `r"You've hit your usage limit\. Visit https://chatgpt\.com/codex/settings/usage"` | escalate | high |
| `You've hit your usage limit. To get more access now, send a request to your admin` | `r"You've hit your usage limit\. To get more access now"` | escalate | high |
| `Try again at ${time}` / ` or try again at ${time}` | `r"\bor try again at \b"` | wait | high |
| `Quota exceeded. Check your plan and billing details.` | `r"Quota exceeded\. Check your plan and billing details"` | escalate | high |
| `To use Codex with your ChatGPT plan, upgrade to Plus: https://chatgpt.com/explore/plus.` | `r"upgrade to Plus: https://chatgpt\.com/explore/plus"` | escalate | high |
| **`Your account was flagged for potentially high-risk cyber activity and this request was routed to gpt-5.2 as a fallback. To regain access to gpt-5.3-codex, apply for trusted access: https://chatgpt.com/cyber or learn more: https://developers.openai.com/codex/concepts/cyber-safety`** | `r"flagged for potentially high-risk cyber activity"` | **nudge** (Codex already downgraded the tier itself) | high |
| `This request has been flagged for possible cybersecurity risk.` | `r"flagged for possible cybersecurity risk"` | route | high |
| `Access blocked by Cloudflare. This usually happens when connecting from a restricted region` | `r"Access blocked by Cloudflare"` | escalate | high |
| `Invalid image in your last message. Please remove it and try again.` | `r"Invalid image in your last message"` | route | high |
| `Previous response was not found. Retrying the full request.` | `r"Previous response was not found\. Retrying the full request"` | nudge | high |
| `Responses websocket connection limit reached (60 minutes). Create a new websocket connection to continue.` | `r"websocket connection limit reached \(60 minutes\)"` | nudge | high |
| `Failed to retry with a faster model: ` | `r"Failed to retry with a faster model"` | unknown | medium |
| `interrupted (Ctrl-C). Something went wrong? Hit `/feedback` to report the issue.` | `r"interrupted \(Ctrl-C\)"` | escalate (user-initiated) | high |
| `internal error; agent loop died unexpectedly` | `r"agent loop died unexpectedly"` | escalate | high |
| `Turn error: ` (TUI prefix that precedes most of the above) | `r"^Turn error: "` | unknown (read the tail) | high |
| `agent thread limit reached` | `r"agent thread limit reached"` | escalate | high |

Codex's internal error `Display` impls, which get concatenated into whatever the TUI
prints, are: `api error `, `stream error: `, `retryable error: `, `rate limit: `,
`invalid request: `, `cyber policy: `. Its wire-level code enum is
`context_window_exceeded | session_budget_exceeded | usage_limit_exceeded |
server_overloaded | cyber_policy | http_connection_failed |
response_stream_connection_failed | internal_server_error | unauthorized | bad_request |
sandbox_error | response_stream_disconnected | response_too_many_failed_attempts |
active_turn_not_steerable`. Anchoring on `cyber_policy` / `serverOverloaded` in
`--json` output is more reliable than prose matching if the orchestrator can use
Codex's JSON mode.

### Moonshot Kimi Code 0.34.0

Source file for every row:
`/opt/homebrew/lib/node_modules/@moonshot-ai/kimi-code/dist/main.mjs`

Kimi ships an explicit machine-readable error registry, `KIMI_ERROR_INFO`, in which
**the vendor itself marks each code retryable or not**. Rendered form (from
`formatStartupError`) is `error: ${info.title}` on one line, a blank line, `message:`,
then the raw provider message.

| Verbatim literal | Proposed Python regex | Action | Confidence |
|---|---|---|---|
| `error: Provider connection error` (registry: `retryable: true`, action `Check network connectivity and retry.`) | `r"error: Provider connection error"` | nudge | high |
| `error: Provider rate limit` (registry: `retryable: true`, action `Retry after a delay or reduce request frequency.`) | `r"error: Provider rate limit"` | wait | high |
| `error: Provider API error` (registry: `retryable: false`, action `Inspect details.statusCode / details.requestId; check provider status.`) | `r"error: Provider API error"` | escalate | high |
| `error: Provider filtered response` (registry: `retryable: false`, action `Revise the prompt or model configuration to avoid provider safety filtering.`) | `r"error: Provider filtered response"` | **route** | high |
| `error: Provider authentication error` (registry: `retryable: false`, action `Re-authenticate with the provider.`) | `r"error: Provider authentication error"` | escalate | high |
| `error: Login required` (action `Run the login flow for the provider before retrying.`) | `r"error: Login required"` | escalate | high |
| `error: Context window overflow` (registry: `retryable: true`, action `Compact the conversation or start a new session.`) | `r"error: Context window overflow"` | route | high |
| `error: Turn exceeded max steps` (action `Increase loop_control.max_steps_per_turn in config.toml or split the task.`) | `r"error: Turn exceeded max steps"` | escalate | high |
| `error: Agent is busy` (`retryable: true`) | `r"error: Agent is busy"` | nudge | high |
| `error: Invalid configuration` / `error: No model configured` / `error: Invalid model configuration` | `r"error: (Invalid configuration\|No model configured\|Invalid model configuration)"` | escalate | high |
| `error: MCP server startup failed` (`retryable: true`) | `r"error: MCP server startup failed"` | nudge | high |
| `error: failed to ${op}: ${msg}` (non-Kimi errors) | `r"^error: failed to "` | unknown | medium |
| `Rate limited...` (swarm status label; `PHASE_LABELS.suspended`) | `r"^Rate limited\.\.\.$"` | wait | high |
| `Provider rate limit; subagent requeued for retry.` | `r"Provider rate limit; subagent requeued for retry"` | nudge (self-healing) | high |
| `Connection error.` / `Request timed out.` / `Request was aborted.` (OpenAI-SDK base messages Kimi re-emits) | `r"^(Connection error\.\|Request timed out\.\|Request was aborted\.)$"` | nudge | medium |
| `Failed.` / `Aborted.` / `Cancelled.` (swarm labels) | `r"^(Failed\.\|Aborted\.\|Cancelled\.)$"` | unknown | medium |

**Documented, not extracted — Kimi Code server-side messages.** The rows above are from
the bundle. The rows *below* are from Moonshot's own error reference
(<https://www.kimi.com/code/docs/en/kimi-code/error-reference.html>) and I did **not**
find them in `main.mjs`, because they originate server-side and Kimi Code passes them
through. Moonshot documents the rendered form as
`error, status code: {N}, message: {text}`. Treat these as high-quality vendor claims
about what appears on screen, one evidentiary step below the bundle-extracted rows.

| Documented literal | Proposed Python regex | Action | Confidence |
|---|---|---|---|
| `error, status code: 429, message: The engine is currently overloaded, please try again later` | `r"The engine is currently overloaded"` | nudge (doc says "retry directly") | high |
| `error, status code: 429, message: We're receiving too many requests at the moment. Please wait a moment and try again.` | `r"We're receiving too many requests"` | wait | high |
| `error, status code: 429, message: You've reached your usage limit for this period. Your quota will be refreshed in the next period.` | `r"usage limit for this period"` | wait (5-hour rolling window) | high |
| `error, status code: 429, message: You've reached kimi monthly usage limit for this billing cycle. Your quota will be refreshed in the next cycle.` | `r"reached kimi monthly usage limit"` | escalate | high |
| `error, status code: 403, message: You've reached your usage limit for this billing cycle. Your quota will be refreshed in the next cycle.` | `r"usage limit for this billing cycle"` | escalate | high |
| `error, status code: 400, message: The request was rejected because it was considered high risk` | `r"rejected because it was considered high risk"` | route | high |
| `error, status code: 400, message: total message size 5943865 exceeds limit 2097152` | `r"total message size \d+ exceeds limit \d+"` | route | high |
| `error, status code: 400, message: Invalid request: Your request exceeded model token limit: 262144 (requested: 558009)` | `r"exceeded model token limit: \d+"` | route | high |
| `error, status code: 401, message: The API Key appears to be invalid or may have expired.` | `r"API Key appears to be invalid or may have expired"` | escalate | high |
| `error, status code: 401, message: Invalid Authentication` | `r"error, status code: 401, message: Invalid Authentication"` | escalate | high |
| `error, status code: 401, message: Your current subscription does not have access to k3. Upgrade to an Moderato plan or above.` | `r"subscription does not have access to k3"` | route (doc offers `kimi-for-coding` as the downgrade) | high |
| `error, status code: 401, message: Your current plan supports only kimi-k3 up to 256K context.` | `r"supports only kimi-k3 up to 256K context"` | route | high |
| `error, status code: 402, message: We're unable to verify your membership benefits at this time.` | `r"unable to verify your membership benefits"` | nudge (doc says "Wait a moment and retry") | high |
| `error, status code: 403, message: Access terminated.` | `r"error, status code: 403, message: Access terminated"` | escalate | high |
| `internal: conn closed` / `internal: driver: bad connection` / `internal: read tcp …: i/o timeout` / `internal: unexpected EOF` | `r"^internal: (conn closed\|driver: bad connection\|unexpected EOF\|read tcp .*i/o timeout)"` | nudge | high |
| `unavailable: 503 Service Unavailable` / `504 Gateway Timeout` / `502 Bad Gateway` | `r"^unavailable: 50[234] "` | nudge | high |
| `canceled: context canceled` (HTTP 499, tool call) | `r"^canceled: context canceled"` | nudge (tool-scoped; "do not affect the conversation itself") | high |

Kimi's quota-exhaustion detector is itself a set of regexes worth stealing verbatim —
`KIMI_QUOTA_EXHAUSTED_ERROR_CODES = {"exceeded_current_quota_error"}` plus
`KIMI_QUOTA_EXHAUSTED_MESSAGE_PATTERNS = [/exceeded your current (?:token )?quota/,
/check your account balance/, /insufficient balance/,
/recharge your account|please recharge/, /account (?:is )?in arrears/]`. A quota-exhausted
429 is deliberately re-mapped to `provider.api_error` (`retryable: false`) rather than
`provider.rate_limit`, with the shipped comment: *"the rate_limit code would re-mint a
rate-limit error across the wire boundary and drive the swarm requeue/suspend loop, which
cannot help until the account is recharged."* That is a `wait` vs `escalate` distinction
the vendor makes explicitly.

### OpenCode 1.17.8

Source file for every row:
`/opt/homebrew/lib/node_modules/opencode-ai/node_modules/opencode-darwin-arm64/bin/opencode`

| Verbatim literal | Proposed Python regex | Action | Confidence |
|---|---|---|---|
| `Provider is overloaded` | `r"^Provider is overloaded$"` | nudge | high |
| `Rate Limited` | `r"^Rate Limited$"` | wait | high |
| `Too Many Requests` | `r"^Too Many Requests$"` | wait | high |
| `${limitName} usage limit reached. It will reset in ${dur}. To continue using this model now, enable usage from your available balance - https://opencode.ai/workspace/${ws}/go` | `r"usage limit reached\. It will reset in "` | wait | high |
| `Free usage exceeded, subscribe to Go` | `r"Free usage exceeded, subscribe to Go"` | escalate | high |
| `Subscribe to OpenCode Go for reliable access to the best open-source models, starting at $5/month.` | `r"Subscribe to OpenCode Go for reliable access"` | escalate | high |
| `Input exceeds context window of this model` | `r"Input exceeds context window of this model"` | route | high |
| `Conversation history too large to compact - exceeds model context limit` | `r"too large to compact - exceeds model context limit"` | route | high |
| `Session too large to compact - context exceeds model limit even after stripping media` | `r"context exceeds model limit even after stripping media"` | route | high |
| `The response was blocked by the provider's content filter` | `r"blocked by the provider's content filter"` | **route** | high |
| `Model did not produce structured output` | `r"Model did not produce structured output"` | nudge | high |
| `Quota exceeded. Check your plan and billing details.` | `r"Quota exceeded\. Check your plan and billing details"` | escalate | high |
| `To use Codex with your ChatGPT plan, upgrade to Plus: https://chatgpt.com/explore/plus.` | `r"upgrade to Plus: https://chatgpt\.com/explore/plus"` | escalate | high |
| `Invalid prompt.` | `r"^Invalid prompt\.$"` | route | medium |
| `Server error.` | `r"^Server error\.$"` | nudge | medium |

OpenCode's retryability rule is inherited from the Vercel AI SDK and is literally
`isRetryable = statusCode != null && (statusCode === 408 || 409 || 429 || >= 500)`.
Its own retry gate then adds: `ContextOverflowError` → never retry; `APIError` with
`!isRetryable && !(status >= 500)` → never retry. Its context-overflow classifier is a
19-pattern regex array — `/prompt is too long/i`, `/exceeds the context window/i`,
`/context[_ ]length[_ ]exceeded/i`, `/model_context_window_exceeded/i`, and 15 more —
which is the single most reusable artifact I found for a cross-provider detector.

### Meta Muse Code 0.1.0-R708.1

Source file for every row: `/Users/colin/.local/bin/muse-bin-0.1.0-R708.1`

Muse is the thinnest result. Its provider layer emits short prefixed messages rather
than composed user-facing sentences, so several rows are lower-confidence about what
actually lands in the pane.

| Verbatim literal | Proposed Python regex | Action | Confidence |
|---|---|---|---|
| `attempt ${n} retrying in ${dur}` | `r"attempt \d+ retrying in "` | nudge (self-healing) | medium |
| `model stream ended before terminal event` | `r"model stream ended before terminal event"` | nudge | high |
| `stream ended before terminal event (` | `r"stream ended before terminal event"` | nudge | high |
| `provider summarizer stream ended before completed` | `r"summarizer stream ended before completed"` | nudge | medium |
| `stream protocol error: ` | `r"stream protocol error: "` | nudge | medium |
| `stream protocol error: SSE frame exceeded ${n} bytes` | `r"SSE frame exceeded \d+ bytes"` | escalate | medium |
| `transport error [request_id=… trace_id=…]` | `r"^transport error \["` | nudge | medium |
| `transport error: ` | `r"^transport error: "` | nudge | medium |
| `response failed (status ${n})` | `r"response failed \(status \d+\)"` | unknown | medium |
| `response incomplete: ` | `r"response incomplete: "` | nudge | medium |
| `API error ` | `r"^API error "` | unknown | low |
| `authentication failed: ` | `r"^authentication failed: "` | escalate | high |
| `model provider `${p}` is not available` | `r"model provider `.+` is not available"` | escalate | high |
| `${X} is temporarily unavailable while a problem with it is fixed. It will come back on its own — there is no setting to change.` | `r"is temporarily unavailable while a problem with it is fixed"` | wait | high |
| `usage limited` / `limited by budget` (run-phase labels) | `r"^(usage limited\|limited by budget)$"` | wait | medium |
| `model task cancelled` / `run cancelled before background compaction installed` / `cancelled during model step` | `r"^(model task cancelled\|cancelled during model step)"` | escalate | medium |
| `Latest error: ` (agent-tree panel field) | `r"Latest error: "` | unknown | medium |
| `Retry or skip: ` (controls hint shown next to a failed child) | `r"Retry or skip: "` | nudge | medium |
| `reasoning effort none is not supported on the meta provider; choose ` | `r"is not supported on the meta provider"` | escalate | high |
| `feedback upload rate limited — try again in ` | `r"feedback upload rate limited"` | wait (telemetry only, not the agent loop) | high |

I found **no** safety-classifier refusal string in the Muse binary. The `refusal` hits
are all part of a scope-discipline reminder template shipped in Muse's own prompt
library, not provider error rendering.

---

## A reusable classifier already shipped in Claude Code

Worth flagging for the detector itself. Claude Code 2.1.224 ships an agent-status
classifier that does exactly the job being built here, at
`/Users/colin/.local/share/claude/versions/2.1.224`. Two pieces:

A code→state map (function `D3d`):

```
authentication_failed → {state:"blocked", needs:"login required — run /login"}
oauth_org_not_allowed → {state:"blocked", needs:"org disabled OAuth — use API key or ask admin"}
billing_error         → {state:"blocked", needs:"usage limit reached — check plan"}
rate_limit            → {state:"blocked", needs:"rate limited — wait and retry"}
overloaded            → {state:"blocked", needs:"API overloaded — wait and retry"}
server_error          → {state:"blocked", needs:"API unavailable — retry"}
invalid_request       → /\b(too long|too large|exceeds|token limit|prompt is too long)\b/i
                        ? "request too large — /compact or trim"
                        : "invalid API request — see detail"
max_output_tokens     → null
undefined             → {state:"blocked", needs:"API error — see detail"}
unknown  (default)    → {state:"failed",  needs:"API error"}
```

And a prose regex (branch `auth-prose`) for the same conditions read off a pane:

```
\b(not logged in|please run \/login|authentication failed|invalid api key|
oauth token (?:expired|revoked)|credit balance (?:is )?too low|usage limit reached|
mcp (?:server )?(?:authentication|auth|authorization|unauthorized)|
mcp (?:server )?(?:credential|token) (?:missing|expired|invalid)|
401 unauthorized|403 forbidden|token (?:has )?expired|bad credentials|
gh auth login|gcloud auth login|aws (?:sso )?login)\b
```

Its accompanying instruction is the design rule you probably want:
*"API/AUTH/INFRA ERRORS → always 'blocked' (transient or user-fixable), never 'failed'.
Set needs to the fix."* And: *"Agent hit an error but is retrying or investigating
('let me try again', 'checking the logs') → 'working'"* — i.e. do not fire the detector
while a `· Retrying in … · attempt n/max` line is on screen.

---

## Table 2 — vendor guidance

Every URL in this table I retrieved myself and read. All are the vendor's own
documentation — no blog summaries, no third-party write-ups. Where a claim is
load-bearing I quote it verbatim.

### The tier-downgrade question, answered first

This was the headline question, and the answer is **yes for Anthropic and OpenAI —
explicitly, in their own docs — and no for Moonshot and z.ai**. Both of the yeses
describe an *automatic* downgrade the vendor performs server-side, not merely advice to
the user.

**OpenAI — YES.** From <https://developers.openai.com/codex/concepts/cyber-safety>:

> "In addition to safety training, automated classifier-based monitors detect signals of
> suspicious cyber activity and **route high-risk traffic to a less cyber-capable model
> (GPT-5.2)**."

> "Developers and security professionals doing cybersecurity-related work or similar
> activity that could be mistaken by automated detection systems **may have requests
> rerouted to GPT-5.2 as a fallback**."

> "The latest alpha version of the Codex CLI includes in-product messaging for when
> requests are rerouted."

That last sentence is the doc-side confirmation of the Codex literal in Table 1. The
documented path back to full capability is Trusted Access
(`https://chatgpt.com/cyber`), not a client-side workaround.

**Anthropic — YES.** From
<https://support.claude.com/en/articles/16049681> ("Why Claude switched models in your
conversation with Opus 5"):

> "A narrow set of higher-risk cybersecurity requests **fallback to Opus 4.8, our
> next-most-capable model**, so we can keep supporting everyday security work while
> limiting the risk of misuse."

> "**Automatic model switching is active by default.** When your request falls back,
> Claude re-runs your blocked Opus 5 request on a less capable model in the same
> conversation. You'll see a notice explaining that the model switched, and the response
> will be labeled with the model that answered."

And the sentence that matters most for a stop-detector:

> "**With automatic model switching off, a request that falls back pauses the
> conversation instead of switching models.**"

The Fable 5 article (<https://support.claude.com/en/articles/15363606>) says the same
and names the targets:

> "When requests are blocked, they may fallback to a non-Mythos model, currently **Opus 5
> for biology, chemistry, and life sciences requests, and Opus 4.8 for offensive
> cybersecurity technique requests**."

> "If your request is also blocked on the less capable model, you can edit your message
> and retry."

Anthropic separately documents a manual model switch as remediation in the Claude Code
error reference (<https://docs.claude.com/en/docs/claude-code/errors>), for the
non-interactive case:

> "**Policy checks vary by model, so switching to a different model with `--model` may
> also resolve the refusal in some cases.**"

**Moonshot — NO.** Its documented remedy for a safety block is to change the prompt, not
the model. From <https://www.kimi.com/code/docs/en/kimi-code/error-reference.html>:

> "`error, status code: 400, message: The request was rejected because it was considered
> high risk` … Review your prompt for sensitive content and retry after modification."

Moonshot *does* document a downgrade, but for **entitlement**, not safety — e.g. "If you
prefer not to upgrade, keep using the standard model `kimi-for-coding`". Do not conflate
the two.

**z.ai — NO.** Error 1301's documented remedy is prompt modification only:

> "1301 / 400 / System detected potentially unsafe or sensitive content in input or
> generation. Please avoid using prompts that may generate sensitive content. Thank you
> for your cooperation." — <https://docs.z.ai/api-reference/api-code>

**Muse — no public error documentation found at all.** Meta ships no developer error
reference for Muse Code that I could locate.

### The table

| Vendor | Condition | Documented remediation | Tier-downgrade fallback officially documented? | Primary source URL |
|---|---|---|---|---|
| Anthropic | `529 overloaded_error` | "The API is temporarily overloaded." Retryable; SDKs "automatically retry transient failures … with exponential backoff, twice by default, honoring the `retry-after` header." Claude Code doc adds: "Run `/model` and switch to a different model to keep working, since capacity is tracked per model." | Yes, for capacity — quoted at left | <https://docs.claude.com/en/api/errors>, <https://docs.claude.com/en/docs/claude-code/errors> |
| Anthropic | `429 rate_limit_error` | "Your account has hit a rate limit." Retryable by SDK. Acceleration limits: "ramp up your traffic gradually and maintain consistent usage patterns." | n/a | <https://docs.claude.com/en/api/errors> |
| Anthropic | `500 api_error` | "Retry the request with exponential backoff; if the error persists, contact support with the request ID." Retryable. | n/a | <https://docs.claude.com/en/api/errors> |
| Anthropic | `504 timeout_error` | "Consider using the streaming Messages API for long-running requests." Retryable. | n/a | <https://docs.claude.com/en/api/errors> |
| Anthropic | `400 invalid_request_error`, `401 authentication_error`, `402 billing_error`, `403 permission_error`, `404 not_found_error`, `413 request_too_large` | Terminal — each documented with a config/account fix, none with a retry instruction. `409 conflict_error` is the one 4xx explicitly marked "Resolve the conflict, then retry the request." | n/a | <https://docs.claude.com/en/api/errors> |
| Anthropic | Mid-stream SSE error after HTTP 200 | "error handling doesn't follow these standard mechanisms." Claude Code doc: retries a drop/stall that lands *before* any output; **does not** retry one that lands after a completed text block or tool call, because "Claude Code could execute the same tool calls twice if it re-ran the request" — it keeps the partial output and appends the `mid-response` notice. | n/a | <https://docs.claude.com/en/api/errors>, <https://docs.claude.com/en/docs/claude-code/errors> |
| Anthropic | TLS certificate validation failure | Explicitly **not** retried: "Claude Code reports the error on the first attempt, so you can fix the certificate setup right away." Transient TLS conditions such as a handshake timeout still are. | n/a | <https://docs.claude.com/en/docs/claude-code/errors> |
| Anthropic | Safety classifier flags a request (Opus 5 / Fable 5) | Automatic fallback to the next-most-capable model, on by default; with it off "a request that falls back **pauses the conversation**". If the fallback is also blocked: edit and retry, or apply to the Cyber Verification Program. | **Yes** — quoted above | <https://support.claude.com/en/articles/16049681>, <https://support.claude.com/en/articles/15363606> |
| Anthropic | Cyber safeguards block (Opus/Sonnet) | Apply to the Cyber Verification Program; appeals process documented. Note the limit: "Prohibited use activities (e.g., mass data exfiltration, ransomware code development) **remain blocked regardless of CVP status**." | No — CVP is access-granting, not a downgrade | <https://support.claude.com/en/articles/14604842-real-time-cyber-safeguards-on-claude> |
| Anthropic | Usage Policy refusal (Claude Code) | Esc-twice / `/rewind` to a checkpoint, or `/clear`. "The check evaluates the full conversation, not only your latest prompt, so sending a new message in the same session usually re-triggers the same refusal." In `-p` mode: "switching to a different model with `--model` may also resolve the refusal in some cases." | Partially — quoted at left | <https://docs.claude.com/en/docs/claude-code/errors> |
| OpenAI | Cyber classifier flags account/request | Automatic reroute to GPT-5.2; regain GPT-5.3-Codex via Trusted Access at `chatgpt.com/cyber`; report false positives via `/feedback`. | **Yes** — quoted above | <https://developers.openai.com/codex/concepts/cyber-safety> |
| OpenAI | `insufficient_quota` (429) | "Quota exceeded. Check your plan and billing details." Terminal — Codex marks it `isRetryable: false`. | No | Rendered literal in Codex 0.146.1; OpenAI error-code docs indexed at <https://developers.openai.com/api/docs/guides/error-codes> |
| OpenAI | `server_is_overloaded` / `server_error` | Retryable — Codex marks these `isRetryable: true`. | Codex additionally renders "Selected model is at capacity. Please try a different model." | Rendered literal in Codex 0.146.1 |
| OpenAI | `usage_not_included` | "To use Codex with your ChatGPT plan, upgrade to Plus." Terminal. | No | Rendered literal in Codex 0.146.1 |
| OpenAI | `context_length_exceeded` | "Codex ran out of room in the model's context window. Start a new thread or clear earlier history before retrying." Terminal without operator action. | No | Rendered literal in Codex 0.146.1 |
| Moonshot | `engine_overloaded_error` (429) | "Wait as indicated by `Retry-After`, reduce concurrency, and retry with exponential backoff. **This is caused by server-side capacity; topping up or upgrading your tier does not resolve it.**" Retryable — "retry directly". | No | <https://platform.kimi.ai/docs/api/errors>, <https://www.kimi.com/code/docs/en/kimi-code/error-reference.html> |
| Moonshot | `rate_limit_reached_error` (429) | Concurrency / RPM / TPM / TPD variants. "Retry after waiting for the time indicated in the response" or upgrade tier. Retryable with wait. | No | <https://platform.kimi.ai/docs/api/errors> |
| Moonshot | `exceeded_current_quota_error` (429) | "Check your balance and billing status" / "Top up your account". Explicitly **not** retryable: "quota errors are account usage issues — **retrying is pointless**, wait for reset or upgrade your plan." | No | <https://www.kimi.com/code/docs/en/kimi-code/error-reference.html> |
| Moonshot | 5-hour rolling quota (429) | `You've reached your usage limit for this period. Your quota will be refreshed in the next period.` Wait for reset or upgrade. | No | <https://www.kimi.com/code/docs/en/kimi-code/error-reference.html> |
| Moonshot | Content safety (400) | `The request was rejected because it was considered high risk` → "Review your prompt for sensitive content and retry after modification." | **No** — prompt change, not model change | <https://www.kimi.com/code/docs/en/kimi-code/error-reference.html> |
| Moonshot | 401 authentication | "**Do not retry**; fix the credentials and resend the request." | No | <https://www.kimi.com/code/docs/en/kimi-code/error-reference.html> |
| Moonshot | 401/403 permission (no k3, no 1M, no highspeed) | Upgrade plan — *or* "If you prefer not to upgrade, keep using the standard model `kimi-for-coding`". "**Retrying is pointless**." | Yes, but for **entitlement**, not safety | <https://www.kimi.com/code/docs/en/kimi-code/error-reference.html> |
| Moonshot | 500 internal / 502/503/504 downstream | "Wait a moment and retry (start with 1 second, up to 3 retries)." Retryable. | No | <https://www.kimi.com/code/docs/en/kimi-code/error-reference.html> |
| z.ai | 1305 (429) service overloaded | `The service may be temporarily overloaded, please try again later` — retryable. General guidance: "Implement exponential backoff retry mechanisms." | No | <https://docs.z.ai/api-reference/api-code>, <https://docs.z.ai/guides/develop/http/introduction> |
| z.ai | 1302 (429) | `Rate limit reached for requests` — retryable with wait. Concurrency is tiered Max > Pro > Lite. | No | <https://docs.z.ai/api-reference/api-code>, <https://docs.z.ai/devpack/usage-policy> |
| z.ai | 1308 / 1316 / 1317 / 1318 (429) | `Usage limit reached for {n} {unit}. Your limit will reset at {next_flush_time}` and the 5-hour / 7-day / monthly-spend-cap variants. Wait for the stated reset. | No | <https://docs.z.ai/api-reference/api-code> |
| z.ai | 1313 (429) Fair Usage Policy | Request frequency limited; "To restore access, please submit a request." Needs a human. | No | <https://docs.z.ai/api-reference/api-code> |
| z.ai | 1113 (429) | `Insufficient balance or no resource package. Please recharge.` Terminal. | No | <https://docs.z.ai/api-reference/api-code> |
| z.ai | 1301 (400) content safety | `System detected potentially unsafe or sensitive content in input or generation. Please avoid using prompts that may generate sensitive content.` | **No** | <https://docs.z.ai/api-reference/api-code> |
| z.ai | 1261 (400) | `Prompt too long` — terminal without trimming. | No | <https://docs.z.ai/api-reference/api-code> |
| z.ai | 1000/1001/1003/1005 (401) | Authentication failed / token expired → regenerate. Terminal. | No | <https://docs.z.ai/api-reference/api-code> |
| z.ai | 1200 / 1230 / 1234 (500) | `API Call Error` / `API call process error` / `Network error, error id: ${error_id}, please try again later` — retryable. | No | <https://docs.z.ai/api-reference/api-code> |
| Meta Muse | any | **No public error documentation located.** | Unknown | — |

### Two vendor docs that are themselves detector-grade

Worth reading in full rather than trusting my summary:

- **Anthropic's Claude Code error reference**, <https://docs.claude.com/en/docs/claude-code/errors>,
  documents the on-screen strings directly, including a table keyed by the literal — e.g.
  "`API Error: Repeated 529 Overloaded errors` | Server errors", "`Connection closed
  mid-response` / `Response stalled mid-stream` | Server", "`<model>'s safeguards flagged
  this message` | Request errors". It also documents the retry banner verbatim: "the
  spinner shows a `Retrying in Ns · attempt x/y` countdown after an error label", and
  the pre-retry banner "`Waiting for API response · will retry in … · check your
  network`" which appears after 20s of stream silence and explicitly "hasn't failed
  yet". A detector that fires on that banner would be firing on a healthy request.
- **Moonshot's Kimi Code error reference**, <https://www.kimi.com/code/docs/en/kimi-code/error-reference.html>,
  publishes the rendered form as `error, status code: {N}, message: {text}` with a
  keyword→status lookup table, and — usefully — warns that "If you are using a
  third-party client such as OpenCode or Claude Code, the client may transform or
  re-wrap error codes… **focus on the text content of the error message**". That is
  exactly the design constraint this detector operates under.

---

## What I could not find, and where I looked

**Not installed on this machine:**

- `cursor-agent`, `copilot` / `gh-copilot`, `kiro`, `amp`, `aider`, `goose`, `windsurf`,
  `zed`. Checked: `command -v` on `$PATH`; `npm root -g`
  (`/opt/homebrew/lib/node_modules`); both nvm node roots
  (`~/.nvm/versions/node/v22.22.2` and `v22.23.1/lib/node_modules`); `~/.bun/install/global`
  (empty); `~/.local/share/pnpm` (absent); `~/.local/bin`; `~/.local/share/uv/tools`;
  `brew list --formula` and `--cask`; `gh extension list` (empty); `/Applications` and
  `~/Applications`; Homebrew python 3.14 site-packages.
- **Antigravity is installed but has no agent CLI.** `/Applications/Antigravity IDE.app`
  and `/Applications/Antigravity.app` are present. The only executable entry point is
  `/Applications/Antigravity IDE.app/Contents/Resources/app/bin/antigravity-ide`, which is
  the stock VS Code launcher shell script (its header still reads
  `Copyright (c) Microsoft Corporation`). Its agent runs inside the Electron UI and never
  renders provider errors to a tmux pane, so it is out of scope for this detector rather
  than "missing".
- **Gemini CLI 0.38.2 is installed** at
  `/opt/homebrew/Cellar/gemini-cli/0.38.2/libexec/lib/node_modules/@google/gemini-cli/bundle/gemini.js`.
  It was not on the request list so I only sampled it. Its bundle is a thin launcher;
  the error strings live in a separate `@google/gemini-cli-core` package. The one useful
  thing visible in the launcher is that it distinguishes
  `event.status === "RESOURCE_EXHAUSTED"` (severity `error`) from everything else
  (severity `warning`), and special-cases `errorCode === "AGENT_EXECUTION_BLOCKED"`,
  both written to **stderr** as `[WARNING] ${event.message}`. If Gemini CLI matters,
  it needs its own pass against the core package.

**From source vs. documented vs. inferred.** Three tiers, and the distinction matters:

1. *Read out of the shipped artifact.* Every row in Table 1 except the one clearly
   labelled block. This is the only tier that tells you what appears on screen. No row
   here was inferred from documentation or from memory of these tools.
2. *Documented by the vendor as the on-screen text.* The labelled Kimi block, plus
   Anthropic's Claude Code error reference, which independently corroborates 12 of my
   Claude Code rows (`API Error: Repeated 529 Overloaded errors`, both `mid-response`
   variants, both `while thinking` variants, `Retrying in Ns · attempt x/y`,
   `Waiting for API response · will retry in … · check your network`, `Prompt is too
   long`, `<model>'s safeguards flagged this message`, `Server is temporarily limiting
   requests`, `Request rejected (429)`, `Credit balance is too low`). Where a binary
   literal and a vendor doc agree, confidence is as high as it gets without running the
   tool.
3. *Documented that the error exists.* All of Table 2's HTTP/error-code rows. These tell
   you a condition exists and whether the vendor considers it retryable. They do **not**
   tell you what the pane shows, because every CLI re-wraps them — a point Moonshot
   makes itself.

The
`${...}` placeholders are real template holes in the shipped code, and where the hole is
filled by a minified constant I resolved it (`cE` → `"API Error"`, `l0s` → the
"intentionally broad safeguards" sentence, `Gan` → `"Repeated 529 Overloaded errors"`,
`Ezu`/`wzu` → the Cyber Verification Program pair, `Z2t` → the limit-name table). The
**action** column is my classification, not the vendor's, except for Kimi where the
`retryable` boolean is the vendor's own field and I have said so.

**What to distrust.**

1. *Presence in the binary is not proof of reachability.* A literal can be behind a
   gate, a server-side feature flag, or a provider that isn't in use. Claude Code's
   refusal-fallback path is explicitly gated on a `switchModelsOnFlag` setting and a
   `convolute_arcades` server flag; the Fable-5 credit messages only fire on plans that
   have Fable. I did not execute any CLI to force these paths, so I have not seen a
   single one of these strings actually on screen.
2. *The pane may wrap or truncate.* Claude Code truncates non-verbose error text to a
   character budget and appends `…`; Codex's TUI wraps to terminal width. Regexes must
   not assume a whole sentence survives on one line. Prefer the short distinctive head
   of each literal, which is how I wrote them.
3. *Unicode.* Claude Code uses `·` (U+00B7) as its separator and `—` (U+2014) in several
   messages, and the source encodes them as `\xB7` / `—`. Match on the real
   characters, and be aware that a terminal capture may normalize or drop them.
4. *Version drift.* These are five specific builds. Claude Code alone shipped five
   versions in the past two weeks on this machine (2.1.220 → 2.1.224). Anything keyed
   to exact prose will rot; the structural anchors (`API Error:` prefix, Codex's
   `cyber_policy` code, Kimi's `error: ${title}` shape, OpenCode's `Provider is
   overloaded`) will last longer.
5. *Muse rows are the weakest.* Its strings are Rust `thiserror` fragments; I inferred
   the concatenated user-facing form from adjacency in the string table rather than
   from a format call I could read. Treat medium/low confidence there as genuine
   uncertainty.
6. *The OpenAI rows in Table 2 lean on the Codex binary more than on OpenAI's API error
   docs.* I retrieved OpenAI's cyber-safety page directly and quote it; for the
   per-code retryability I used the `isRetryable` booleans Codex itself ships, which is
   arguably better evidence for this purpose but is not the same thing as OpenAI's
   published error-code reference. If you need OpenAI's own table, it is indexed under
   Operations → Error codes at `developers.openai.com`.
7. *The `nudge` classification on the two safety-fallback rows is deliberate and could
   be argued.* When Codex prints "routed to gpt-5.2 as a fallback" or Claude Code prints
   "Switched to … Your session model is unchanged", the downgrade has *already happened*
   and the turn produced an answer — so "continue" is correct and no routing decision is
   needed. But the answer came from a weaker model, which the orchestrator may care
   about. Consider emitting a `nudge` plus a quality flag rather than a bare `nudge`.
8. *The single highest-value line in this whole document, for a stop-detector, is
   Anthropic's:* "With automatic model switching off, a request that falls back **pauses
   the conversation** instead of switching models." That is the exact silent-stall state
   this detector exists to catch, and it is configuration-dependent — the same safety
   block produces a self-healing turn on one machine and an indefinite hang on another.
   Check `switchModelsOnFlag` before assuming which behaviour you are detecting.
