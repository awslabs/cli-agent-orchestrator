# Provider version policy

**Unpinned is the correct state. A pin is an incident artifact.**

Every harness — Claude Code, Codex, Kimi, Muse — runs at whatever version is
installed, with full capability, without being added to a list first. New builds
bring capability far more often than they break the narrow slice of behaviour
this system drives, so withholding a feature until a build is proven costs more
than it protects.

A pin exists only while a specific observed breakage is open. It is temporary,
recorded, and attached to the work that removes it.

---

## 1. The two things called "pinning"

They are different, and only one is ever legitimate for long.

| | what it means | default |
|---|---|---|
| **Capability allowlist** | a feature is withheld unless the installed build is listed | **not used** |
| **Version quarantine** | a named build is known broken, so it is held back | **empty** — populated only by an open incident |

They fail in opposite directions. An allowlist excludes **every future build by
default**, so it expires the moment the vendor ships and the system degrades by
standing still — a vendor release silently removes a capability nobody chose to
give up. A quarantine excludes only what has been shown to break, so a new build
is trusted until it earns suspicion and the list shrinks as fixes land.

**Prefer a quarantine. Do not introduce a new capability allowlist.** Writing
"the version must be in this tuple" adds an expiry date to a feature.

---

## 2. Modes and the runtime lever

`provider_contracts.py` declares each provider's enforcement mode.

- **`open`** — any non-empty semver-shaped observed version is accepted, and gets
  full capability. This is the default for every provider.
- **`strict`** — exact-set membership in `SUPPORTED_VERSIONS`. A build must be
  listed to launch at all. This is the quarantine mode: an opt-in containment for
  a **reproduced** regression, never the normal update policy.

Either can be forced at runtime with no code change:

```bash
CAO_PROVIDER_VERSION_ENFORCEMENT_KIMI=strict
# Same form for CODEX, CLAUDE, MUSE. Values: strict | open.
```

The variable is `CAO_PROVIDER_VERSION_ENFORCEMENT_<PROVIDER>` using the short
provider name. **This is the pin lever** — the whole mechanism, already present
and reversible without a deploy.

---

## 3. Per-version knowledge is an override, never a precondition

Some behaviour genuinely varies by build: a composer newline keystroke, a submit
settle interval, an emptiness probe, an image transport. Recording what was proven
on a specific build is valuable and stays.

What changes is how a *missing* record is treated:

- **A missing per-version record selects a conservative default and proceeds.**
- It does not withhold the feature, and it does not silently select an unsafe
  value.

The second half is not a detail. A default is a claim about behaviour on an
unknown build, so it must be the safe end of the range rather than the null value.
This repository contains the case that motivates the rule: a build with no proven
composer pin received `submit_settle_seconds: 0.0` against a renderer this
codebase documents as swallowing an Enter that arrives too soon, "leaving the
message sitting unsubmitted in the prompt box with no error." An absent
measurement became a silent non-delivery. Use the longest proven interval as a
floor instead, and record that the value is a floor rather than evidence.

Where no default can be made safe, **that single operation** refuses with a typed
reason naming what is missing, and the rest of the provider stays available. A
missing composer pin must not cost the provider its identity, its routing, or its
ability to receive an operator message. §6 holds the two operations that cannot be
defaulted *with the technique we use today*, which is a limitation to remove rather
than a category to grow.

---

## 4. When a pin is acceptable

All four must hold:

1. **A reproduced breakage on the new build.** A flake, a transient network
   error, or a one-off rendering timing difference is not a version regression.
   Absence of proof is not evidence of breakage.
2. **The same operation confirmed working on a previous build**, isolating the
   failure to the new binary rather than to environment or task state.
3. **A P0 tracker issue** naming the failure, the build that exhibits it, and the
   build being pinned to. P0 because a pin is a capability ceiling on the whole
   installation, and its severity is the pin's age, not the symptom's.
4. **A stated unpin criterion and a named owner.** "When the bug is fixed" counts
   only if the issue says what fixed means.

The issue's work is fixed in shape: unpin and reproduce the breakage
deterministically, find the fix, confirm deterministically that it mends the
breakage, then land the fix and remove the pin **in the same change**, so the
system is back on the latest build the moment it can be. A pin exists only for as
long as it takes to find someone with the capacity to do that.

Pinning to work around something that **cannot be fixed yet** is legitimate and
expected. Pinning to avoid finding out is not.

A pin with no issue behind it is an unexplained capability ceiling and should be
deleted on sight. A pin whose issue is closed is stale and should be deleted on
sight — that needs no ceremony beyond verifying against the build that broke.

---

## 5. Who may pin

| actor | may pin | rationale |
|---|---|---|
| operator | yes | it is their installation |
| fire marshal | yes | it owns get-it-running-again, is invoked deliberately, and records its actions as incident artifacts by construction |
| supervisors | **no** | a supervisor sees a symptom, not a cause; a misattributed pin freezes a harness for a reason that was never true and outlives the session that set it |
| implementers, reviewers, workers | no | they report the breakage |

**A pin is a fact about a provider build, never about a role.** There is no
supervisor pin, implementer pin, or spec-writer pin, and no capability is withheld
from one kind of worker and granted to another on version grounds. A build either
honours a contract or it does not, and that answer does not change with the job the
worker was given.

**What a supervisor does instead:** work around it, record what it saw, avoid that
harness for the rest of the run, and keep going. A lane that reports cleanly and
routes around a problem is a good outcome; stalling to await a version decision
is not.

---

## 6. Capability is proven at runtime, against the installed binary

No surface withholds a capability because a build is unlisted. Where a capability
claim is stronger than "the process started" — a resumable pre-task identity, a
zero-task route attestation — the claim is **verified while it is made**, on the
binary in front of the process.

That is strictly stronger evidence than an allowlist. A table records that someone
tested a different build on a different day; a runtime proof records that *this*
build did the thing, just now.

**Codex native bind.** The zero-turn app-server bootstrap mints the pre-task
identity and proves the whole contract as it goes: `initialize -> initialized ->
config/read -> thread/start -> thread/name/set -> clean process exit` with no
`turn/*`, every response present and error-free, the exact project resolved as
`trusted`, a canonical UUID, the exact cwd and route, exactly one materialized
rollout, and a byte-identical protected user config afterwards.

One element of that contract the minting process cannot establish about itself:
`thread/name/set` materializes the rollout inside a single app-server lifetime, so
a mint that never leaves that lifetime has shown the rollout exists — not that
anything else can open it. A **fresh process** therefore runs `thread/resume` on
the minted id and must be handed back the same id, non-ephemeral, before the
receipt claims resumability. It sends no `turn/*` and writes no task bytes, and it
re-checks the protected config, because a proof bought with a side effect nobody
asked for is not a proof.

The bind seam requires that proof on the readiness receipt. A build that cannot
honour the contract fails on the contract, with zero task bytes, rather than on its
version string — and a build nobody has listed is never refused for that reason
alone.

**Codex zero-task route attestation.** The trust probe validates the exchange it
runs by the same standard, so it needs no separate list either. It observes and
records the version; it does not test the version for membership.

Muse's managed native profile carrier is verified at launch by a two-leg
runtime probe (`muse exec --provider echo` with and without base instructions)
against the resolved inner binary rather than an allowlist of build digests.
A `disproved` build — one that ran a clean turn with base instructions
present, so it ignores them — fails closed, because that is the state which
silently produces a worker running the vendor's default persona. An `unproven`
verdict proceeds: the probe established nothing, and refusing on it would block
every managed launch for ordinary machine load while protecting against a
sequence that is barely reachable, since a build that ignores the file exits
zero and is already `disproved`.

What `unproven` must not do is claim otherwise. The verdict and its reason
travel on the capability block and the acquisition receipt, so a reader sees
that nothing was established rather than an assertion nobody made. A persistent
`disproved` is cleared deliberately by setting
`CAO_MUSE_PROFILE_CARRIER_PROVEN` to the sha256 of the resolved
`muse-bin-<revision>` and restarting the conductor, which records an operator
attestation as `probed_by_operator` rather than lifting the gate.

The probe measures `muse exec --provider echo`; the launch it gates is the
interactive TUI. That is a proxy, and it is the strongest zero-cost signal
available — it exercises the same `TBH_EVAL_APPEND_SYSTEM_PROMPT_FILE` surface
on the same inner binary. It does not prove the TUI honours that surface, and
no claim in the receipt or refusal says otherwise.

---

## 7. Un-pinning

A pin is reviewed for as long as it exists, and the review asks one question: is
the fix landed?

1. Fix the underlying defect.
2. Verify against the build that exhibited the breakage.
3. Remove the pin and its entry in the same change.
4. Close the issue, naming the verification.

**A pin with no active fix effort is escalated, not renewed.** Either fix it or
decide explicitly that the capability is abandoned. Quietly carrying the pin is
the outcome to avoid: stale pins accumulate, and each one makes the next upgrade
look riskier than it is.

---

## 8. Fail-closed invariants

These hold regardless of mode, because each is a case where proceeding would
record something untrue rather than merely act on less knowledge:

- An unknown provider name raises `ProviderContractError`.
- An unparseable version banner raises `ProviderVersionDrift`. Unparseable is not
  the same as unlisted: the first means the observation failed, the second means
  nothing was written down.
- A Codex readiness receipt without a fresh-process resume-adoption proof cannot
  become a managed generation's bound native identity, whatever the launch mode
  admitted. The proof is evidence the launch produced, never a version lookup.
- An unrecognised screen or response is `unknown`, never `complete`.

---

## 9. Conformance, surface by surface

Every capability allowlist is converted. A build absent from every per-feature
table now gets capability from a conservative default or a runtime read of the
installed bundle, never a withheld feature.

| surface | how a missing record resolves |
|---|---|
| `SUPPORTED_VERSIONS` | no longer a capability gate; the strict-mode quarantine set only. Consumers ask whether the version was *observed*, not whether it is listed |
| `_PROVEN_COMPOSER_NEWLINE` | keystroke derived from the installed bundle, version-bound to the build being driven; settle takes the cond-0480 floor and records `submit_settle_proven: False` |
| `_PROVEN_STEER_CHORDS` | derived from the installed bundle; unreadable keeps the loud zero-POST refusal |
| `_RENDERED_SESSION_PROVEN_BUILDS` | derived when the bundle shows the title rewrite and the header labels, recorded as bundle-derived; a changed layout yields no proof and the attachment freezes loudly |
| `IMAGE_PROVEN_BUILDS` | advertised for any observed build, with `build_proven` recording live acceptance separately |
| `PINNED_VERSIONS` | advisory everywhere |
| Codex native bind | the bootstrap proves the contract at runtime, including a fresh-process `thread/resume` adoption; no build is listed |
| Codex route attestation | the trust probe validates its own exchange; the version is observed and recorded, never tested for membership |

**Bundle-derived is its own evidence tier, below `observed`.** A derived record says
the installed bundle asserts this, not that anyone watched it work. Plans and receipts
carry the source (`composer_keystroke_source`) so a reader can tell a derived value
from a live proof, per §3's rule that a mechanism records only what it established.

### Where no safe default exists, and why

**Codex's composer newline.** The 0.147.0 binary carries `insert_newline` and
"Insert a newline in the editor." but **no** `ctrl+j` / `ctrl-j` string — its keymap
defaults are compiled structured data rather than readable text. There is no hint to
derive and no safe constant, because a wrong keystroke types junk into the composer or
submits mid-message. So that one operation keeps its typed refusal on an unlisted Codex
build while the rest of the provider stays open, which is exactly the §3 carve-out.

Claude and Kimi hints were verified readable on the builds installed when this landed
(Claude Code 2.1.233, Kimi 0.36.1). **Muse is not currently derivable.** Its 0.1.0-R708.1
layout was readable; 0.2.1-R1215.1 moved the key run and the description into separate
tables 1339 bytes apart, and the extractor requires them adjacent. The outcome is a typed
refusal with nothing typed, so Muse multi-line delivery is refused exactly as it was
before — no regression, and the loud direction.

That update landed **six minutes after this conversion was written**, by the self-updating
launcher, with nobody involved. Treat it as the standing evidence for §1: a readability
measurement names a build and expires with it, so state the build you measured and expect
to re-measure rather than recording a fact about "the installed build" in the abstract.

### Still gating on an exact build or launcher layout

Muse's managed profile carrier is gated on Meta's launcher layout
(`.muse-version` + `muse-bin-<revision>`), not on a build digest, and it is a
runtime probe rather than a list — see §6.

Nothing else does. Every capability allowlist is converted: a build absent from
every per-feature table gets capability from a conservative default, a runtime read
of the installed bundle, or a runtime proof of the contract itself.

## 10. Deliberately not built yet

Recorded so they are not mistaken for oversights, and not built until the
operator asks.

- **Supervisor-initiated pinning.** Deferred for the misattribution reason in §5.
  Revisit only with evidence that a supervisor can identify a version as the
  culprit rather than merely being present when something failed.
- **Supervisor-initiated fire marshal wake.** Plausibly useful, gated on a
  genuinely-stuck test: a supervisor that can still route around a problem should.
  A wake that fires whenever a lane is inconvenienced turns a break-glass role
  into a routine dependency.
- **Automatic fixer dispatch on a recorded breakage.** The recording comes first.
- **A `conduct` verb wrapping the env-var lever**, so a quarantine can carry its
  issue, unpin criterion, and owner as data rather than as operator memory, and
  so `conduct status` can surface every live pin with its age. Tracked as M18.
