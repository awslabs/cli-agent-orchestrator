# Provider error strings in the CAO terminal-log corpus

Corpus: `/Users/colin/.aws/cli-agent-orchestrator/logs/terminal/*.log` — 107 files, 1.4 GB raw.
Method: full-corpus streaming ANSI strip (`\x1b[...` CSI/OSC + C0 controls removed, split on `\r` and `\n`),
producing 932,884 rendered screen segments (88 MB). All counting and aggregation was done over that
cleaned corpus; only matched regions were read.

Working files (scratchpad, disposable): `.../scratchpad/work/{clean.py,scan2.py,agg.py,ctx.py,count.py,clean/}`.

## Provider census (this matters for the "absence of evidence" findings)

`terminals` in the SQLite DB has only 5 surviving rows (all `codex`), so provider was taken from the
per-terminal `*.snapshot.json` sidecars (74 files, authoritative `provider` field) and, for the 33 logs
with no snapshot, inferred from the CLI's own UI chrome.

| provider | logs | how |
|---|---|---|
| `codex` | 86 | 68 snapshot + 18 inferred (Codex footer `gpt-5.6-… · Context N% left · weekly N% left · 258K window`, `• Ran`, `└`, `Working(Ns • esc to interrupt)`) |
| `claude_code` | 14 | 3 snapshot + 11 inferred (`⏺`, `⎿`, `✻`, `❯`, `Compacted (ctrl+o …)`, `Claude Code v2.1.220` banner) |
| `kimi_cli` | 7 | 3 snapshot + 4 inferred (`Welcome to Kimi Code!` box, or `[managed worker] provider=kimi_cli`) |
| `muse_cli` | **0** | — |
| `opencode_cli` | **0** | — |

**`muse_cli` and `opencode_cli` have no logs at all in this corpus.** Every `glm-5.2` / `GLM-5.2` /
`opencode` hit (≈450 across 12 files) is a Codex or Claude supervisor *writing prose about* routing to
them — never a pane running them. We have zero rendered error strings for those two providers, and any
detector rule for them would be invented, not observed.

Two rendering modes exist and they look completely different:
* **raw TUI capture** (`pipe-pane` of an alt-screen CLI) — Codex/Claude/Kimi TUI, heavy repaint.
* **managed / ACP view** — line-oriented `[provider event] …`, `[provider diagnostic] …`,
  `[managed worker] …`, `[turn completed]`. 54 codex logs and all managed Kimi logs are this mode.
  A screen detector reading `capture-pane` on a *managed* pane sees the `[provider …]` lines, not TUI chrome.

---

## Findings, ordered by occurrences descending

`occurrences` = number of rendered fragments matched in the cleaned corpus. Because these are raw
pipe-pane captures of a repainting TUI, one real event is usually re-rendered many times; the
`files` column and the notes give the honest event-level picture.

---

### 1. Claude Code — turn interrupted

| field | |
|---|---|
| provider | `claude_code` |
| verbatim | `⎿  Interrupted · What should Claude do instead?` |
| regex | `r'(?m)^\s*⎿\s*Interrupted\s*·\s*What should Claude do instead\?\s*$'` |
| action | **nudge** |
| occurrences | 909 fragments, 12 files |
| evidence | `/Users/colin/.aws/cli-agent-orchestrator/logs/terminal/643c2043.log:4` (also `7b7b127c.log:4`, `6697b7ef.log:5`, `3c40861b.log:5`) |
| reasoning | The turn was cut mid-flight and the pane is idle with prior work preserved; re-sending the instruction resumes it. **Caveat:** in this corpus these are overwhelmingly *operator/orchestrator-initiated* ESC, not provider failures, so a detector must not treat this alone as evidence of a recoverable provider error — pair it with a preceding error banner or with "no operator input in the last N seconds". |

---

### 2. Codex — background model-list refresh timeout (diagnostic)

| field | |
|---|---|
| provider | `codex` (managed/ACP logs) |
| verbatim | `[provider diagnostic] 2026-08-03T21:13:52.636908Z ERROR codex_models_manager::manager: failed to refresh available models: timeout waiting for child process to exit` |
| regex | `r'ERROR codex_models_manager::manager: failed to refresh available models:\s*(?P<detail>.+)$'` |
| action | **unknown** (treat as ignore) |
| occurrences | 339, 6 files |
| evidence | `.../0b52a074.log:659`; also `0d618fcf.log:4845`, `37eb5e29.log:2160` |
| reasoning | Emitted by a background model-catalog refresher while the turn keeps running — it never coincides with an idle pane in this corpus, so nudging on it would fire hundreds of false positives. |

---

### 3. Codex — MCP transport worker died

| field | |
|---|---|
| provider | `codex` |
| verbatim | `[provider diagnostic] 2026-07-28T23:51:41.797665Z ERROR rmcp::transport::worker: worker quit with fatal: Transport channel closed, when Client(HttpRequest(HttpRequest("http/request failed: error sending request for url (http://127.0.0.1:3141/)")))` |
| regex | `r'ERROR rmcp::transport::worker: worker quit with fatal: Transport channel closed'` |
| action | **escalate** |
| occurrences | 253, **54 files** |
| evidence | `.../0308f0af.log:2`; also `5b38e8e2.log`, `eaaef738.log`, `a16f7b1d.log` |
| reasoning | A local MCP server (`127.0.0.1`) is down — a config/infra fault the model cannot fix by continuing; it appears at startup in essentially every managed codex session, so it is chronic rather than acute. |

---

### 4. Codex — skills context budget truncation

| field | |
|---|---|
| provider | `codex` |
| verbatim | `⚠ Skill descriptions were shortened to fit the 2% skills context budget. Codex can still see every skill, but some descriptions are shorter. Disable unused skills or plugins to leave more room for the rest.` |
| regex | `r'(?m)^\s*⚠\s*Skill descriptions were shortened to fit the\s*(?P<pct>\d+)% skills context budget'` |
| action | **escalate** (config), non-blocking |
| occurrences | 126, 7 files |
| evidence | `.../1cac282f.log:46`; also `d8ebaba9.log:724`, `47b61be3.log:20692` |
| reasoning | Informational config pressure, not a stop; only worth acting on if it correlates with degraded behaviour. |

---

### 5. Codex — MCP client failed to start

| field | |
|---|---|
| provider | `codex` |
| verbatim | ``⚠ MCP client for `anki` failed to start: MCP startup failed: handshaking with MCP server failed: Send message error Transport`` (followed by `[rmcp::transport::worker::WorkerTransport<…StreamableHttpClientWorker<…>>] error: Client error: HTTP request failed: http/request failed: error sending request for url (http://127.0.0.1:3141/), when send initialize request`) |
| regex | ``r'(?m)^\s*⚠\s*MCP client for `(?P<server>[^`]+)` failed to start:\s*(?P<detail>.+)$'`` |
| action | **escalate** |
| occurrences | 112, 24 files |
| evidence | `.../069d6929.log:126`; also `26f5c069.log:214`, `65a43164.log:3033` |
| reasoning | A named MCP server is unreachable; continuing will keep failing the same tool, so it needs a human/infra fix. |

---

### 6. Claude Code — file-edit tool failure

| field | |
|---|---|
| provider | `claude_code` |
| verbatim | `⎿ Error editing file` |
| regex | `r'(?m)^\s*⎿\s*Error editing file\s*$'` |
| action | **unknown** (treat as ignore) |
| occurrences | 113, 4 files |
| evidence | `.../1ad645c8.log:4`; also `3606da38.log:4`, `6697b7ef.log:5`, `a3e1fcef.log:93` |
| reasoning | A tool-level error the model handles inside its own turn — the pane does not go idle, so it is not a screen-detector signal. |

---

### 7. Codex — MCP startup incomplete

| field | |
|---|---|
| provider | `codex` |
| verbatim | `⚠ MCP startup incomplete (failed: anki)` |
| regex | `r'(?m)^\s*⚠\s*MCP startup incomplete \(failed:\s*(?P<servers>[^)]*)\)'` |
| action | **escalate** |
| occurrences | 109, 24 files |
| evidence | `.../069d6929.log:130`; also `128ab359.log:32`, `85ba9173.log:100` |
| reasoning | Same root cause as #5, rendered as a startup summary; a config problem, not something "continue" resolves. |

---

### 8. Codex — "Additional safety checks" blocking interstitial ★

| field | |
|---|---|
| provider | `codex` |
| verbatim | Overlay title `Additional safety checks`, body `This request requires additional safety checks, which can take extra time. Hang tight or retry with a faster model for a quicker response, though it may be less capable of handling complex requests.`, choices `› 1. Retry with a faster model` / `2. Keep waiting` / `3. Keep… Learn more`, footer `Press enter to confirm or esc to go back` |
| regex | `r'(?s)Additional safety checks.{0,120}?This request requires additional safety checks, which can take extra time\.\s*Hang tight or retry with a faster model'` (choice list: `r'(?s)1\.\s*Retry with a faster model.{0,200}?2\.\s*Keep waiting.{0,200}?3\.\s*Learn more'`) |
| action | **wait** |
| occurrences | 53 rendered banner fragments (92 including supervisor prose), 1 file |
| evidence | `.../13e6fe47.log:2858` (banner), `.../13e6fe47.log:2712` (choice list); wedge events logged at 14:03:02, 14:03:43, 14:20:29, 14:25:25 EDT plus several validator panes |
| reasoning | Transient provider-side latency gate that self-clears; the campaign's own verified recovery is the structured **option 2 "Keep waiting"**, never option 1. **Important: "continue" will not clear this — it needs a menu selection (`2` + Enter).** Choosing option 1 silently downgrades the model. |

---

### 9. Codex — model list refresh, stream disconnected

| field | |
|---|---|
| provider | `codex` |
| verbatim | `[provider diagnostic] 2026-08-06T06:02:11.278792Z ERROR codex_models_manager::manager: failed to refresh available models: stream disconnected before completion: error sending request for url (https://chatgpt.com/backend-api/codex/models?client_version=0.146.0)` |
| regex | `r'ERROR codex_models_manager::manager: failed to refresh available models: stream disconnected before completion: error sending request for url \((?P<url>[^)]+)\)'` |
| action | **unknown** (treat as ignore) |
| occurrences | 88, 3 files |
| evidence | `.../0b52a074.log:2763`; also `0d618fcf.log:4845`, `37eb5e29.log:1911` |
| reasoning | Real transport drop, but on the background catalog fetch, not on the turn stream — the turn continues, so it is not an idle-stop signal. It *is* a useful "network is flaky right now" prior. |

---

### 10. Claude Code — MCP server needs authentication

| field | |
|---|---|
| provider | `claude_code` |
| verbatim | `⚠ 1 MCP server needs authentication · run /mcp` |
| regex | `r'(?m)^\s*⚠\s*(?P<n>\d+)\s*MCP servers?\s*needs?\s*authentication\s*·\s*run\s*/mcp\s*$'` |
| action | **escalate** |
| occurrences | 68, 14 files |
| evidence | `.../1ad645c8.log:4`; also `a63b0174.log:4`, `7b7b127c.log:4`, `423a8d0f.log:1` |
| reasoning | An OAuth/credential flow needs a human at a browser; no amount of nudging changes it. |

---

### 11. Codex — usage-limit resets available

| field | |
|---|---|
| provider | `codex` |
| verbatim | `• You have 1 usage limit reset available. Run /usage to use one.` (also `… 2 usage limit resets available …`) |
| regex | `r'(?m)^\s*•\s*You have\s*(?P<n>\d+)\s*usage limit resets?\s*available\.\s*Run /usage to use one\.'` |
| action | **wait** |
| occurrences | 65, 18 files |
| evidence | `.../069d6929.log:124`; also `128ab359.log:53`, `65a43164.log:21` |
| reasoning | A quota affordance rendered at session start, not a stop; it signals the account is near a limit, so the safe response is to wait for the window rather than burn a reset. |

---

### 12. Codex — weekly limit heads-up

| field | |
|---|---|
| provider | `codex` |
| verbatim | `⚠ Heads up, you have less than 25% of your weekly limit left. Run /status for a breakdown.` |
| regex | `r'(?m)^\s*⚠\s*Heads up, you have less than\s*(?P<pct>\d+)% of your weekly limit left\.\s*Run /status for a breakdown\.'` |
| action | **wait** |
| occurrences | 43, 11 files |
| evidence | `.../13e6fe47.log:49252`; also `1cac282f.log:13166`, `d8ebaba9.log:7`, `65a43164.log:1953` |
| reasoning | Quota exhaustion warning with a stated weekly window — the right move is to stop spending and wait for reset (or reroute), not to nudge. |

---

### 13. Codex — conversation interrupted

| field | |
|---|---|
| provider | `codex` |
| verbatim | ``■ Conversation interrupted - tell the model what to do differently. Something went wrong? Hit `/feedback` to report the issue.`` |
| regex | `r'(?m)^\s*■\s*Conversation interrupted - tell the model what to do differently\.'` |
| action | **nudge** |
| occurrences | 37, 8 files |
| evidence | `.../069d6929.log:1205`; also `85ba9173.log:14221`, `912c3acb.log:482`, `13e6fe47.log:41966` |
| reasoning | Codex's counterpart to #1: the turn ended early and the pane is idle awaiting input; re-stating the instruction resumes it. Same caveat — usually ESC-initiated in this corpus. |

---

### 14. Claude Code — safety-classifier block with automatic model switch ★

| field | |
|---|---|
| provider | `claude_code` (Fable 5 / Opus) |
| verbatim | `⏺ Fable 5's safeguards flagged this message. The safeguards are intentionally broad right now and may flag safe and routine coding, cybersecurity, or biology work. These measures let us bring you Mythos-level capabilities sooner, and we're working to refine them. Switched to Opus 4.8. Send feedback with /feedback or learn more` — followed by `⎿  Tip: You can configure model switch behavior in /config` |
| regex | `r"(?m)^\s*(?:⏺\s*)?(?P<model>[\w .'-]{2,20})'s safeguards flagged this message\.\s*The safeguards are intentionally broad"` plus optional `r'Switched to (?P<new_model>[\w .-]+)\.'` |
| action | **route** |
| occurrences | 37 fragments, 2 files (**2 distinct events**) |
| evidence | `.../a63b0174.log:4` and `.../d2dceff0.log:4` |
| reasoning | A safety classifier rejected the request for that model and the CLI has already changed the route out from under you — the detector's job is to notice the route changed (the footer flips to `opus-4-8`) and decide whether the new model is acceptable, not to say "continue". |

---

### 15. Codex — tool router error (exec / apply_patch)

| field | |
|---|---|
| provider | `codex` |
| verbatim | ``[provider diagnostic] … ERROR codex_core::tools::router: error=apply_patch verification failed: Failed to find expected lines in …`` ; ``… error=exec_command failed for `/bin/zsh -lc '…'`: CreateProcess { message: "Rejected(\"Failed to create unified exec process: No such file or directory (os error 2)\")" }`` ; `… error=timeout_ms must be at least 1000` ; `… error=Full-history forked agents inherit the parent agent type; omit agent_type, or spawn without a full-history fork.` |
| regex | `r'ERROR codex_core::tools::router: error=(?P<detail>.+)$'` |
| action | **unknown** (treat as ignore) |
| occurrences | 23, 15 files |
| evidence | `.../0308f0af.log:220`; also `c54fa9fb.log:323`, `a16f7b1d.log:1054`, `f0c8eea4.log:606` |
| reasoning | Model-visible tool errors that the model reacts to inside its own turn — the pane keeps working. |

---

### 16. Claude Code — connection closed mid-response ★ (the canonical case)

| field | |
|---|---|
| provider | `claude_code` |
| verbatim | `⏺ API Error: Connection closed mid-response. The response above may be incomplete.` |
| regex | `r'(?m)^\s*(?:⏺\s*)?API Error:\s*Connection closed mid-response\.(?:\s*The response above may be incomplete\.)?\s*$'` (generic sibling: `r'(?m)^\s*(?:⏺\s*)?API Error(?:\s*\((?P<paren>[^)]*)\))?:\s*(?P<detail>.+?)\s*$'`) |
| action | **nudge** |
| occurrences | 13 rendered fragments across 3 Claude Code logs (**≥3 distinct stop events**); 5 further mentions are Codex supervisors writing *about* it, not rendering it |
| evidence | `.../a3e1fcef.log:5`, `.../a63b0174.log:4`, `.../1ad645c8.log:4` |
| reasoning | Transport drop mid-stream; in every observed instance the pane returned to the idle `❯` prompt with partial work preserved and resumed after a "Continue…" message — exactly the nudge case. Confirmed in `1ad645c8.log`, where the very next pane content is the operator's `Continue the exact assigned … revision from the current worktree and current diff. First inspect the last edit boundary because the provider response closed mid-response`. |

---

### 17. Codex — hard content refusal / cybersecurity block ★

| field | |
|---|---|
| provider | `codex` |
| verbatim | ```ⓘ This content can't be shown``` / `We take extra caution with cybersecurity requests. If you're a security professional, you may be able to apply for Trusted Access.` / `Trusted Access: https://openai.com/form/enterprise-trusted-access-for-cyber/` / `Learn more: https://help.openai.com/en/articles/20001326` |
| regex | `r"(?m)^\s*(?:ⓘ\s*)?This content can'?t be shown\s*$"` — confirm with `r'We take extra caution with cybersecurity requests\.'` or `r'Trusted Access: https://openai\.com/form/enterprise-trusted-access-for-cyber/'` |
| action | **route** |
| occurrences | 12 (`This content can't be shown`) + 2 (`Trusted Access:` URL line), 1 file; **≥3 distinct events** (PR-C validator twice, supervisor once, plus validator `79d5a2b8`) |
| evidence | `.../13e6fe47.log:5823` and `.../13e6fe47.log:7889` |
| reasoning | A persistent classifier refusal for the current model/turn: Codex emits `task_complete` with no report and no callback, returning to idle. The corpus records that progress resumed **only after a human `/model` switch to `gpt-5.6-terra xhigh` plus continue** — i.e. a model change, not a nudge. Distinct from #8, which is transient. |

---

### 18. CAO / provider version gate

| field | |
|---|---|
| provider | `codex` and `kimi_cli` (emitted by CAO's managed launcher into the pane) |
| verbatim | `[managed-provider-blocked] unsupported provider version 'codex-cli 0.146.0'; expected one of ['codex-cli 0.145.0']` (older form: `… 'codex-cli 0.145.0'; expected 'codex-cli 0.144.6'`); also `[managed-provider-blocked] 'socket'` |
| regex | `r"(?m)^\[managed-provider-blocked\]\s*(?P<detail>.+)$"` |
| action | **escalate** |
| occurrences | 11, 7 files (5 of the 7 are logs whose *entire content* is this one line) |
| evidence | `.../1460caeb.log:1`; also `198cdda6.log:1`, `53fe8aaf.log:1`, `c6ae0374.log:1`, `8e68c544.log:1111` |
| reasoning | The worker never started; a pinned-version mismatch needs a config/deploy change by a human. |

---

### 19. Claude Code — automatic retry banner ★

| field | |
|---|---|
| provider | `claude_code` |
| verbatim | `✻ API error · Retrying in 1s · attempt 1/10` · `✻ Request timed out. · Retrying in 0s · attempt 9/10` · `✻ Request timed out. · Retrying in 37s · attempt 8/10` · `✻ 529 Overloaded · Retrying in 3s · attempt 3/10` |
| regex | `r'(?m)^\s*(?:[✻✽✳✶✢◐●*]\s*)?(?P<reason>[^·\n]{2,60}?)\s*·\s*Retrying in\s*(?P<delay>\d+)s\s*·\s*attempt\s*(?P<n>\d+)/(?P<max>\d+)\s*$'` |
| action | **nudge** (do **not** act while `n < max`) |
| occurrences | 14 fragments, 3 files — reason breakdown: `Request timed out.` ×7, `API error` ×5, `529 Overloaded` ×2 |
| evidence | `.../1ad645c8.log:4` (all three reasons), `.../fc5f7a20.log:5`, `.../635d63c1.log:5` |
| reasoning | The CLI is *already* self-healing on a 10-attempt ladder — the correct detector behaviour is to recognise the pane is busy, suppress the stall timer, and only escalate if `attempt` reaches `max` or the banner is replaced by an `API Error:` line (#16). |

---

### 20. Codex — model at capacity ★

| field | |
|---|---|
| provider | `codex` |
| verbatim | `⚠ Selected model is at capacity. Please try a different model.` |
| regex | `r'(?m)^\s*⚠\s*Selected model is at capacity\.\s*Please try a different model\.\s*$'` |
| action | **route** |
| occurrences | 4 fragments, 1 file (2 distinct events) |
| evidence | `.../7864d54f.log:17573` and `.../7864d54f.log:18790` |
| reasoning | The provider itself names the remedy ("try a different model") — a capacity ceiling on the selected model, so switch model rather than retry the same one. |

---

### 21. Codex — resumed with a different model

| field | |
|---|---|
| provider | `codex` |
| verbatim | ``⚠ This session was recorded with model `gpt-5.6-terra` but is resuming with `gpt-5.6-sol`. Consider switching back to `gpt-5.6-terra` as it may affect Codex performance.`` |
| regex | ``r'(?m)^\s*⚠\s*This session was recorded with model `(?P<recorded>[^`]+)` but is resuming with `(?P<current>[^`]+)`'`` |
| action | **route** |
| occurrences | 4, 1 file |
| evidence | `.../65a43164.log:1037` |
| reasoning | Route drift on resume — the pane is running a different model than the campaign assigned, which is a routing correction, not a transient failure. |

---

### 22. Codex — "Approaching rate limits" model-downgrade interstitial ★

| field | |
|---|---|
| provider | `codex` |
| verbatim | `Approaching rate limits` / `Switch to gpt-5.4-mini for lower credit usage?` / `› 1. Switch to gpt-5.4-mini   Small, fast, and cost-efficient model for simpler coding tasks.` / `2. Keep current model` / `3. Keep current model (never show again)   Hide future rate limit reminders about switching models.` / `Press enter to confirm or esc to go back` |
| regex | `r'(?s)Approaching rate limits\s*Switch to\s*(?P<suggested>[\w.-]+)\s*for lower credit usage\?'` |
| action | **route** |
| occurrences | 2, 2 files (variants suggest `gpt-5.4-mini` and `gpt-5.6-luna`) |
| evidence | `.../13e6fe47.log:54261` and `.../65a43164.log:1176` |
| reasoning | Quota pressure surfaced as a blocking numbered prompt whose only outcomes are a model change or an explicit decline — it needs a routing decision (and a keystroke), not "continue". Both observations occur when the footer already reads `weekly 5% left`. |

---

### 23. Kimi Code — turn cancelled / tool failed

| field | |
|---|---|
| provider | `kimi_cli` (managed/ACP view) |
| verbatim | `[turn completed] cancelled` ; `[tool] tool — failed` |
| regex | `r'(?m)^\[turn completed\]\s*(?P<reason>cancelled|end_turn)?\s*$'` ; `r'(?m)^\[tool\]\s*(?P<name>.+?)\s*—\s*failed\s*$'` |
| action | **unknown** |
| occurrences | `cancelled` ×2 (1 file); `— failed` ×6 (2 files) |
| evidence | `.../f9b2da58.log:235` (cancelled); `.../8e68c544.log:67` (tool failed) |
| reasoning | These are the only failure-shaped renderings Kimi produces anywhere in the corpus, and neither carries a provider-level cause, so no action can be inferred from the string alone. |

---

### 24. Codex — reconnecting after request timeout

| field | |
|---|---|
| provider | `codex` |
| verbatim | `Reconnecting... 2/5` … `└ Request timed out` (rendered alongside `(58s • esc to interrupt)`) |
| regex | `r'Reconnecting\.\.\.\s*(?P<n>\d+)/(?P<max>\d+)'` and `r'(?m)^\s*└\s*Request timed out\s*$'` |
| action | **nudge** (suppress while `n < max`) |
| occurrences | 1 each, 1 file |
| evidence | `.../13e6fe47.log:2735` |
| reasoning | Codex's own bounded reconnect ladder; like #19 the pane is still busy, so the detector should wait out the ladder and only nudge if it exhausts. One observation only — treat the regex as provisional. |

---

### 25. CAO — worker failed to initialize

| field | |
|---|---|
| provider | orchestrator message rendered into a `codex` supervisor pane (not a provider string) |
| verbatim | `Worker 2715fa85 failed to initialize: TimeoutError('Codex initialization timed out after 60 seconds'). It has been deleted — re-assign the task or report the …` |
| regex | `r"Worker (?P<terminal>[0-9a-f]{8}) failed to initialize: TimeoutError\('Codex initialization timed out after (?P<secs>\d+) seconds'\)"` |
| action | **escalate** |
| occurrences | 1, 1 file |
| evidence | `.../7864d54f.log:46665` |
| reasoning | The worker never came up and CAO already deleted the terminal; there is no pane left to nudge. Included because it *does* appear on a pane the detector will read — it must not be mistaken for a provider error. |

---

## Patterns I could not classify confidently

* **`⎿ Interrupted · What should Claude do instead?` and `■ Conversation interrupted …`** (findings 1, 13). I classified both `nudge`, but I could not establish from the logs whether the interruption was operator-initiated (ESC / an inbound CAO message) or provider-initiated. In `13e6fe47.log` there is an explicit note that a *structured stop* produced "the safeguard switch followed by Interrupted", i.e. the same string with two different causes. These strings are cheap to match and dangerous to act on alone.
* **`[provider event] warning`** — 2 occurrences in `eaaef738.log:56` and one other file, with no payload whatsoever in the rendered stream. Unclassifiable.
* **`[turn completed]` with trailing prose** (e.g. `.../7864d54f.log:25631` `[turn completed] again with no report.md, no conduct report/callback, and no commit.`) — the ACP `[turn completed]` marker is being concatenated with unrelated screen text by the repaint capture, so I cannot tell whether a distinct "abnormal completion" rendering exists.
* **`⚠ Found invalid entries in: .`** (`1cac282f.log:10019`) — appears only inside a diff of Codex `/status` diagnostics output, never as live chrome. No idea what triggers it live.
* **Whether Claude Code has any quota/usage-limit banner.** There is none in the corpus. All 862 "quota / weekly / five-hour / usage limit" lines in the 14 Claude Code logs are the *agent's own prose* about `check-ai-quota` results. So I cannot give you a Claude Code rate-limit regex from evidence, and I am not going to invent one.
* **`kimi_cli` provider errors generally.** Across all 7 Kimi logs (≈2,700 rendered segments) there is not one provider-level failure string. The Kimi TUI logs (`36bf6af6`, `8cee72b1`) are ~95 segments each — startup banner only. This is a genuine coverage hole, not a "Kimi is reliable" finding.

## What a reader should distrust about this corpus

1. **It is `pipe-pane` output, not `capture-pane` output.** ~94% of the 1.4 GB is ANSI cursor-movement, and stripping it concatenates in-place repaints into garbage like `⏺Fble5's safegursflaggedthismessage`. Characters are *dropped mid-word* in the historical logs. **Your detector will not see this** — `capture-pane -p` renders the final screen — so write your regexes against the clean forms quoted above and only loosen them if you need to grep these archives.
2. **Occurrence counts are repaint counts, not event counts.** A single Claude Code `API Error` was re-rendered 3–7 times as the TUI redrew the scroll region. Wherever it mattered I gave both numbers; where I only give one, divide by roughly 3–10 for real events.
3. **The corpus is enormously skewed.** 86 of 107 logs are Codex, and a handful of very long supervisor sessions (`7864d54f` 255 MB, `13e6fe47` 245 MB, `1cac282f` 158 MB, `d8ebaba9` 153 MB) contribute most of the text. Several findings (#8, #17, #22, #24) come from **a single file**, `13e6fe47.log`.
4. **Supervisor prose contaminates naive greps.** Codex/Claude supervisors write long messages *quoting* error strings (`ends with \`API Error: Connection closed mid-response\``, `` `This content cannot be shown` refusal stopped PR-C twice ``, `` `Additional safety checks` prompt with choices 1 Retry faster… ``). Any detector regex must be anchored to line start with the CLI's glyph prefix (`⏺`, `⚠`, `■`, `ⓘ`, `⎿`, `✻`, `•`, `└`) or it will match the agent talking about the error. That is why every regex above is `(?m)^`-anchored.
5. **Provider versions have moved on.** The corpus spans `codex-cli 0.144.6` → `0.145.0` → `0.146.0` (5 logs are nothing but a version-gate refusal), Claude Code `v2.1.220`, Kimi Code `0.29.1`/`0.29.2`. Codex model names in the corpus (`gpt-5.6-sol`, `-terra`, `-luna`, `gpt-5.4-mini`) appear in the interstitial text, so don't hard-code them — capture them.
6. **Two distinct render modes.** Anything matched from a `[provider event]` / `[provider diagnostic]` / `[managed worker]` line came from the managed/ACP view. If the detector reads a managed pane it will see those lines and none of the TUI glyphs; if it reads a raw TUI pane it will see the glyphs and none of the bracketed events. Findings 2, 3, 9, 15, 18, 23 are managed-mode only; 1, 4–8, 10–14, 16, 17, 19–22, 24 are TUI-mode only.
7. **No credentials were quoted.** Session ids, generation UUIDs, commit SHAs and file paths appear in the quoted text; nothing matching an API key, token, bearer, cookie or authorization header was included. One session-bootstrap string (`session/new failed: {'code': -32000, 'message': 'Authentication required'}`, `a3e1fcef.log:93`, 41 fragments) is an auth-failure *class* with no secret in it — it is a CAO/ACP bootstrap failure rather than a provider CLI rendering, so I left it out of the findings table; classify it `escalate` if you want it.
