# ADR-005: Index eligibility and injection eligibility are two policies

**Issue:** [#644](https://github.com/awslabs/cli-agent-orchestrator/issues/644)
**Status:** Proposed. **Revision 3** — the secrets position is reversed by ruling R5, its mode settled by ruling R14, and the curator's single-input property is now stated as the reason R1 is total. Revision 2 changed the enforcement placement; see [Revision 2 correction](#revision-2-correction).
**Decision owner:** the issue's risk "Index eligibility and automatic context injection must be separate, explicit policies"; enforcement placement settled by human ruling R1
**Related:** [design.md](design.md), [adr-003-read-and-write-surface.md](adr-003-read-and-write-surface.md), [adr-007-configuration-surface.md](adr-007-configuration-surface.md)

---

## Context

The issue is unusually direct: "Vault notes may contain secrets, private material,
stale claims, or prompt injection. Index eligibility and automatic context injection
must be separate, explicit policies."

The two things are not the same act.

**Recall** is deliberate and narrow. An agent, or a human at the CLI, issues a query
with a scope and a limit; results are ranked, capped, access-counted, and returned as a
tool result the agent then reasons about. One call, logged, with a query an operator
could inspect.

**Injection** is automatic and broad. A `<cao-memory>` block is assembled and prepended
to a worker agent's prompt without anyone reading it. Nothing in that flow involves a
human deciding that this particular note should influence this particular task.

Three risks make the difference material for vault content:

- **Prompt injection.** A note is prose from outside CAO. A synced vault can receive a
  note written by someone else. Text that reaches an agent's prompt automatically and
  unread is the highest-value place to plant instructions.
- **Confidentiality.** A note may be indexable-and-useful for a targeted query while
  being wrong to broadcast into every agent's context in a session.
- **Staleness.** A stale claim answers a query badly; a stale claim in every prompt
  shapes every decision.

## Revision 2 correction

Revision 1 of this ADR asserted that placing a filter inside
`get_memory_context_for_terminal` made the gate total. **That was wrong, and this ADR
asserted it in three places.** The review's finding F1 is confirmed against source:

- `services/terminal_service.py:118` `inject_memory_context(first_message,
  terminal_id)` is the real injection entry point. It calls
  `get_curated_memory_context(...)` at line 135 and prepends the result; line 1233
  applies it to the outgoing message.
- `get_curated_memory_context` (`memory_service.py:2906`) has two exits. Its **success**
  path — a `memory_manager` curator terminal exists in the session and is idle —
  dispatches the task description to that curator and returns the curator's own
  `<cao-memory>` block **verbatim** at line 2972. It reaches
  `get_memory_context_for_terminal` **only on its fallback path** at line 2976.

So on every successful curated injection, the gate was bypassed entirely. Revision 1's
own rejected-alternative E half-anticipated this by worrying about "a filter every caller
can call with either value" — the worry was right, the conclusion was not.

Human ruling R1 settles the placement, and it reopens what revision 1 rejected as
alternative E. The new arrangement is below; the previously-rejected alternative is now
the accepted mechanism, and the newly-rejected alternative is post-filtering the
curator's output.

## Decision

**Two independent configuration keys. Injection default-deny. Enforcement happens
inside a single candidate chokepoint, which the curator's own recall also passes
through — so what the curator can emit is injectable by construction. A note may be
indexed and not injectable; a note may not be injectable without being indexed.**

### Gate 1 — index eligibility

- Key: `mappings[].index`, default `true` for a mapping that exists (mapping a folder is
  itself the opt-in; see [adr-003-read-and-write-surface.md](adr-003-read-and-write-surface.md)).
- Effect: whether a note gets a `memory_metadata` row, becomes a BM25 candidate, and is
  reachable by `memory_recall`.
- Enforced in `vault/scan.py` and `vault/reconcile.py` — a mapping with `index: false`
  yields no rows at all.

### Gate 2 — injection eligibility

- Key: `mappings[].inject`, default **`false`**, always and regardless of `index`.
- Effect: whether a note may reach an agent's prompt through the injection path.
- `inject: true` with `index: false` is a config-load error: you cannot inject what is
  not indexed, and accepting the combination silently would leave an operator believing
  injection was on.

### Where Gate 2 is enforced

In `vault/reader.py::resolve_candidates`, the **single** function through which every
vault read passes:

```
resolve_candidates(binding, *, keys=None, scope, scope_id, require_injectable: bool)
```

`require_injectable` has **no default**, so no call site can omit it. It is derived from
the caller's context, not chosen freely:

| Caller | `require_injectable` |
| --- | --- |
| Ordinary `memory_recall` / `recall()` | `False` |
| The deterministic builder `get_memory_context_for_terminal`, including its related fan-out at line 2854 | `True` |
| **A recall whose resolved terminal context is the injection curator** | `True` |
| The graph provider (U9) | `False` — graph exposure equals recall exposure |

The curator arm is the R1 mechanism. `get_curated_memory_context` already identifies the
curator with `_find_context_manager_terminal(session_name)`, and a curator terminal is
identified by its `agent_profile`. A recall arriving from that terminal is therefore gated
at the chokepoint, so the curator **cannot see** a non-injectable vault note and cannot
restate what it never read.

**Why gating that one door is total: `memory_recall` is the curator's only memory input.**
The `memory_manager` profile instructs the curator to use `session_context`, and **no such
MCP tool exists** — the only `session_context` in the tree is a provider-internal method at
`providers/kimi_cli.py:1093`, not reachable as a tool. So the curator has exactly one door
into the corpus, and gating it gates everything the curator can know. This is load-bearing
for R1 rather than incidental, and it carries a standing obligation: **if a real
`session_context` tool is ever added it becomes a second curator input and R1 silently
degrades** — the gate would still hold on `memory_recall` while the new tool bypassed it
entirely. Whoever adds such a tool must route it through the same chokepoint with
`require_injectable` derived the same way. Recorded here because the failure would be
invisible: injection would keep working, and would simply stop being gated.

**Fail closed when the curator is unidentifiable.** If the curator terminal cannot be
resolved to a profile, the injection path does not dispatch to it and falls back to the
deterministic builder, which is gated. Unknown provenance is never treated as
injectable.

### The full gate stack

Four independent switches, all of which must permit an action. Enumerated so nobody
assumes two is the whole story:

1. `memory.enabled` — the shipped master switch (`_is_memory_enabled()`, line 76). Off
   means no memory operation at all. The vault path must not bypass it; note that it is
   the **first line** of `get_curated_memory_context`, so a new injection arm that skips
   the service skips the check too.
2. `memory.vault.enabled` — default false.
3. `mappings[].index` — this note may be recalled.
4. `mappings[].inject` — this note may be injected. Default false.

### Observability

`cao memory vault status` prints an explicit warning line naming every mapping with
`inject: true`, with its scope and note count. A default-deny switch that is easy to
flip and invisible afterwards is default-deny in name only.

## Consequences

**Positive.**

- The safe-and-useful configuration is the default: a mapped folder is recallable
  immediately and injected never.
- The gate is **structural rather than asserted**. Because there is one function through
  which vault bytes are reached and its gate parameter has no default, a future caller
  cannot forget it. Revision 1's three separate bypasses (the curator, related-memory
  expansion, the graph projection) were all instances of having several paths and one
  gate; there is now one path.
- Gating the curator's **input** rather than its output means the guarantee survives
  paraphrase, which is the only way it can hold against an LLM intermediary.
- Testable independently, and the tests are the proof the criterion asks for: an
  `index: true, inject: false` note **is** returned by `recall()` and **is not** present
  in either injection path; a teeth test removes the filter and asserts it appears.
- The failure mode of a misconfiguration is reduced capability, not exposure.

**Negative, and accepted.**

- **The curator loses recall breadth.** When producing an injection block, the curator
  cannot see non-injectable vault notes, so its summary is drawn from a smaller corpus
  than a human operator's `memory_recall` would reach. That is the intended trade: the
  curator's output is unread prompt content, so its input must be injectable material
  only.
- **Curator identification becomes load-bearing.** The gate depends on resolving the
  curator terminal to a profile. If that resolution is wrong in either direction the
  consequence is asymmetric but bounded: a false positive over-restricts the curator's
  recall, a false negative would under-restrict it — which is why the unidentifiable
  case fails closed to the deterministic builder rather than dispatching ungated.
- **Two switches to explain.** Documentation must teach the difference between
  recallable and injected.
- **A user who wants vault knowledge in every prompt must opt in per mapping.**
- **Two gates are not three.** There is no per-note opt-out (for example
  `cao.inject: false` in frontmatter). Folder granularity was judged sufficient for
  release one, and a per-note switch invites a false sense of control over notes the
  user has not audited. Noted as a possible follow-up.

## Alternatives rejected

### A. One switch — indexed implies injectable

Rejected. It is what the issue explicitly forbids, and the reason is the asymmetry of
consequence: recall is a query the agent chose to make, injection is text the agent did
not ask for. Collapsing them means the only way to keep a note out of every prompt is to
make it unfindable, which throws away the feature for that note.

### B. Two switches, both defaulting on

Rejected. Injection default-on makes the first `reconcile --apply` a bulk prompt-content
change across every agent in every session, from a corpus the user has not audited
note-by-note. For a directory that plausibly holds confidential material and possibly
other people's writing, the default must be deny.

### C. Implement non-injectability by omitting the note from the index

Rejected, and called out because it is the tempting shortcut — a one-line change in the
scanner instead of a gate on a read path. It silently reunifies the two policies: the
note becomes unrecallable, AC3 is violated for that folder, and the user cannot reach
knowledge they explicitly mapped. A test asserting recall-yes-inject-no is what prevents
a future refactor from taking this shortcut.

### D. A global `memory.vault.inject_enabled` boolean instead of per-mapping

Rejected. Injection risk is not uniform across a vault: a curated `Reference/` folder may
be entirely appropriate to inject while `Meetings/` is not. A single global switch forces
the most sensitive folder's answer onto every folder, which in practice means it stays
off and the capability is unused, or it goes on and the sensitive folder is exposed.

### E. Post-filter the curator's returned block — REJECTED BY RULING R1

This is the alternative that looks like the natural fix once F1 is understood: let the
curator recall whatever it likes, then strip non-injectable content from the
`<cao-memory>` block it returns before prepending it.

Rejected, and the rationale is the whole reason the gate moved to the input side. **The
curator is an LLM that paraphrases and synthesises.** It does not echo note bodies; it
restates them. A filter over generated text can only match on keys, paths or literal
substrings, none of which survive paraphrase — so confidential content restated in the
model's own words passes straight through. The filter would present as a control while
providing none, which is worse than no control at all, because it would end an audit
conversation early.

Gating the input is the only placement where the property is structural: the curator
cannot restate what it never read.

### F. Filter at the `recall()` layer with a caller-supplied `injectable_only` parameter

This was revision 1's rejected alternative E, and revision 2 **partially adopts it** —
which is why it is restated here rather than silently dropped. Revision 1's objection was
sound: "it puts the security decision in a function every caller can call with either
value, so the default becomes whatever each call site passes."

What changed is not the objection but the design that answers it. The parameter now lives
on `resolve_candidates`, not on `recall()`; it has **no default**, so omission is a type
error rather than a silent `False`; and it is **derived from caller context** by the two
injection entry points rather than chosen by arbitrary callers. The chokepoint is what
converts a caller-optional flag into an enforced invariant.

## Security and compliance implications

- **Prompt injection is the primary threat.** Vault prose is untrusted content.
  Default-deny injection means untrusted prose does not reach an agent's prompt
  automatically. When an operator opts a folder in, they are making an explicit trust
  statement about its contents and authorship, and `status` keeps that statement
  visible.
- **The LLM intermediary is part of the threat model.** The curator is not a trusted
  filter; it is an untrusted transformer sitting between the corpus and the prompt.
  Ruling R1's input-side gate is the only placement that accounts for that. Any future
  component that summarises memory before injection inherits the same rule: gate its
  input, never its output.
- **Confidentiality is separable from utility.** An operator can make a
  confidential-but-useful folder recallable — where a targeted query returns it to an
  agent that asked — without broadcasting it into every prompt in every session,
  including sessions whose transcripts may be retained elsewhere.
- **One enforcement site bounds the security review.** The reviewer checks one function
  and its callers' derivation of `require_injectable`, rather than auditing every path
  that might reach a note.
- **Fail-closed on missing binding or missing curator identity.** A candidate whose
  binding cannot be resolved is excluded; a curator that cannot be identified is not
  dispatched to.
- **The gate stack is enumerated so it can be audited.** An operator answering "can this
  note reach an agent's prompt?" checks four named switches in one settings file, three
  of which default to the closed position for vault content.
- **Secrets — position reversed by ruling R5.** Revision 2 of this ADR argued that index
  eligibility should not run the secret gate, on the grounds that gating reads would
  silently hide a note the user mapped on purpose. The ruling accepted that *argument* and
  rejected the *conclusion*, and it is right: the argument was against **silence**, not
  against **gating**, and index-time quarantine with a reported finding gives the
  visibility I wanted while closing the exposure I had left open.
  The gate now runs at **three** points: index time (reconcile), the write path, and
  export. At index time `scan_for_secrets` is applied to the note body; under
  `mappings[].secret_gate: "reject"` (the default) the note is quarantined with a
  `secret_detected` finding, and under `"warn"` it is indexed with the same finding
  recorded. The finding carries the matched **pattern name** only, never the bytes.
  **Ruling R14 settles the mode**: `mappings[].secret_gate` — the same key, the same two
  values — governs the index boundary as well as the write boundary, with `reject` as the
  default. No third value, no second key. The reasoning is worth stating because it is a
  confidentiality decision: `reject` by default keeps the AC10 exposure fix as the
  out-of-the-box posture, while a vault owner who knows their own corpus gets a documented,
  per-mapping way to say *these are examples, not credentials*. That escape hatch is
  necessary rather than indulgent precisely because one of the six patterns is
  `(?i)(?:password|passwd|secret|pwd)\s*[:=]\s*\S{6,}`, and notes **about** credential
  handling — runbooks, design notes, incident write-ups — are ordinary content in a
  knowledge vault.

  **The finding is reported in both modes.** The mode governs whether the note is indexed,
  never whether the operator is told. A `warn` mapping that silently indexed a
  secret-bearing note would be the worst of both designs.

  Two residuals belong here rather than in a footnote. The gate is a **heuristic
  deny-list**, so a false positive under `reject` makes a legitimate note unrecallable —
  which is what `warn` exists to answer. And the gate is **reconcile-time**, so a secret
  added to an already-indexed note is exposed until the next reconcile; that follows from
  the no-continuous-watching non-goal and is surfaced through `index_freshness`.

  Three alternatives were rejected (R14). **Always quarantine with no override** is the
  strongest guarantee and leaves a credential-documenting vault partly unrecallable with no
  remedy short of editing the user's own notes. **`warn` by default** inverts the reason
  AC10 was flagged — the insecure posture must not be the default. **A per-pattern
  confidence policy** — quarantine on `aws_access_key`/`pem_private_key`, warn on the loose
  `password:` regex — is the most precise answer and the natural future refinement, deferred
  because classifying six patterns by confidence makes that classification its own
  security-relevant review surface, better done with evidence about which patterns actually
  misfire.
  The injection policy remains a separate and stricter layer: even under `warn`, a
  secret-bearing note at `inject: false` is recallable by a deliberate query and never
  reaches an agent's prompt automatically. That layering is the point — index eligibility
  and injection eligibility answer different questions, and a note can fail one while
  passing the other.
  **The one pairing worth naming is `secret_gate: "warn"` together with `inject: true`.**
  That is the maximal-exposure combination in the whole schema: a note the gate flagged as
  credential-shaped, indexed anyway, and injected automatically into agent prompts. It is
  **coherent** — unlike `inject: true` with `index: false`, which is a contradiction and a
  hard config error — so it is permitted, but never silently: ADR-007 rule 16 makes it a
  config-load warning and a permanent line in `cao memory vault status`. The distinction
  this design draws is between refusing an *incoherent* configuration and refusing a
  *risky* one; the second is the operator's call to make, and CAO's job is to make sure they
  cannot make it by accident.
- **Forward risk.** A frozen-memory injection arm designed elsewhere deliberately
  bypasses `MemoryService` for determinism. It must route through the chokepoint with
  `require_injectable=True` and must not skip `_is_memory_enabled()`. Recorded so the
  next author inherits the constraint rather than rediscovering it.
