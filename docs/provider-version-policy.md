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
3. **A tracker issue** naming the failure, the build that exhibits it, and the
   build being pinned to.
4. **A stated unpin criterion and a named owner.** "When the bug is fixed" counts
   only if the issue says what fixed means.

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

**What a supervisor does instead:** work around it, record what it saw, avoid that
harness for the rest of the run, and keep going. A lane that reports cleanly and
routes around a problem is a good outcome; stalling to await a version decision
is not.

---

## 6. The last two gates, and why they are work rather than exceptions

Two capability tables still gate on exact builds. **They are not permanent
carve-outs.** A capability that only works on enumerated builds is evidence that
our technique for obtaining it is build-fragile, not evidence that the vendor made
it build-specific. The goal is to remove both by finding a version-robust
technique, and until then they are the honest description of a limitation rather
than a design we defend.

Treat a request to add a build to either table as a signal that the underlying
approach needs a deeper look, and prefer investigating the binding technique over
extending the list.

Codex launch paths capture the pre-task harness-native session id through a
zero-turn app-server bootstrap (`thread/start` + `thread/name/set`, no `turn/*`)
so a resumed TUI can guarantee an exact resumable session before any task byte
reaches the pane. That is a capability claim about the exact binary: the full
exchange — `initialize -> initialized -> config/read -> thread/start ->
thread/name/set -> clean process exit`, canonical UUID, exact cwd/model/effort,
one materialized rollout, fresh `thread/resume` adopting the same id — must have
been verified for that build. Proven builds are in
`NATIVE_BIND_CAPABLE_VERSIONS`, and `codex_native_bootstrap.BOOTSTRAP_CAPABLE_VERSIONS`
is that same table's Codex cell, so the bootstrap that mints an id and the bind
seam that accepts it cannot disagree.

Today this is the §3 case: no conservative default exists, because there is no
safe way to *pretend* a session is resumable, and degrading silently would produce
a launch that cannot resume itself. So this one operation fails closed with a typed
refusal — zero provider initialization, zero task bytes — while everything else
about the provider stays open.

**That is a statement about the current technique, not about Codex.** The exchange
is ordinary app-server protocol; nothing in it is documented as build-specific. The
open question is which step actually varies between builds, and whether the
contract can be *verified at runtime against the installed binary* instead of
looked up in a table. A bootstrap that proves its own guarantee on the build in
front of it needs no allowlist. Removing this gate is tracked work.

Two rules keep the exception from spreading:

- **The bind seam must never consult the broad table.** Doing so reproduced a real
  forward-compatibility failure: a 0.147.0 native launch completed the bootstrap,
  exposed its exact session identity, reported `input_ready`, and was then refused
  at bind.
- **Bind capability grants nothing else by implication.** It is not a step toward
  a broad allowlist.

Muse has a similar narrow cell, and the same question applies: the managed native
profile carrier is enabled only on the exact full banner `Muse Code 0.1.0
(0.1.0-R708.1)` with the stage-proven inner `muse-bin-*` SHA-256. Pinning to an
inner binary digest cannot survive any vendor update by construction, which makes
it the clearest case that the technique — not the version — is what needs fixing. The update-capable `muse` launcher script is never
that evidence. A same-semver R revision or changed inner digest advertises as
`profile_carrier_unverified` until separately verified.

**Adding a build to a narrow table is recording an override, not lifting a gate.**
Verify the contract, add the exact build, and leave the provider's mode `open`.

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
- A build outside `NATIVE_BIND_CAPABLE_VERSIONS` cannot become a managed
  generation's bound native identity, whatever the launch mode admitted.
  Membership grants bind only.
- An unrecognised screen or response is `unknown`, never `complete`.

---

## 9. Conformance, surface by surface

The six capability allowlists are converted. A build absent from every per-feature
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

### Still gating on an exact build

`NATIVE_BIND_CAPABLE_VERSIONS` and the Muse profile-carrier cell, per §6. They cannot
be converted by choosing a default — they need a version-robust technique, which is
research rather than refactor, and it is done: no build has ever failed the Codex
bootstrap contract, and both replacements are zero-cost runtime probes. Tracked
separately. `ROUTE_ATTEST_CAPABLE_VERSIONS` belongs to that same family.

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
