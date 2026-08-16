# Provider version policy

CAO's provider-version policy decides which installed provider builds may cross
the launch identity boundary and which builds may carry feature-specific
authority.  The policy is per-provider, reversible at runtime, and fail-closed
for unknown or unparseable versions.

## Modes

`provider_contracts.py` declares each provider's default enforcement mode:

* **`strict`** — exact-set membership in `SUPPORTED_VERSIONS`.  A build must be
  listed there to launch at all.  This is an opt-in containment mode for a
  provider with a reproduced regression; it is not the normal update policy.

* **`open`** — any non-empty semver-shaped observed version is accepted at the
  launch identity boundary.  The exact `SUPPORTED_VERSIONS` tuple still gates
  feature-specific authority: native control, rendered-session proof,
  steer/composer, image delivery, resume, and route authority.  All providers
  use this mode by default so routine CLI updates do not freeze admission.
  Kimi's current build has additionally passed a compatibility check; other
  builds still require exact feature proof before they receive advanced
  capabilities.

Unknown providers and unparseable versions fail closed in every mode.

## Why two layers?

The launch boundary and the capability boundary answer different questions.

*Launch* asks "can we start a managed process against this binary?"  Routine
  updates should not freeze task delivery just because a pin file has not been
  updated.

*Capability* asks "has this exact build been read or proven for the specific
  feature we are about to use?"  A future semver may launch, but it must not
  silently inherit 0.34.0's composer keystrokes, 0.29.2's image transport, or
  the rendered-header session proof.

Splitting the two prevents both stale-route breakers *and* unproven-build
authority leaks.

## Runtime override

Each provider's mode can be forced at runtime without a code change:

```bash
CAO_PROVIDER_VERSION_ENFORCEMENT_KIMI=strict
# The same form works for CODEX, CLAUDE, and MUSE.
```

Valid values are `strict` and `open`.  The variable name is
`CAO_PROVIDER_VERSION_ENFORCEMENT_<PROVIDER>` where `<PROVIDER>` is the short
provider name (`kimi`, `codex`, `claude`, `muse`).

This is the generic rollback path.  If a future provider build causes a
reproducible regression — for example, a managed launch reaches the TUI but a
multiline composer plan submits incorrectly, or the rendered session proof no
longer matches the bound session — set
set the matching provider variable (for example
`CAO_PROVIDER_VERSION_ENFORCEMENT_KIMI=strict`) to restore exact-pin
fail-closed behaviour while the regression is investigated. File a high
priority CAO issue for the regression and remove the override promptly after
the new build is stage-verified.

## When to switch a provider back to strict

Switch a provider to strict only after a reproducible regression tied to a
specific new build.  The decision checklist applies equally to Kimi, Claude,
Codex, and Muse:

1. **Reproduce the failure on the new build.**  A flake, a transient network
   error, or a one-off rendering timing difference is not a version-policy
   regression.
2. **Confirm the same operation succeeds on a proven build.**  This isolates
   the failure to the new binary rather than to environment or task state.
3. **Set the provider's enforcement variable to `strict`.**  This refuses the
   new build at the launch boundary and restores exact-set behaviour.
4. **Stage-verify the new build before removing the override.**  Read the
   relevant bundle facts, prove the ACP identity contract, and update
   `SUPPORTED_VERSIONS` plus the per-feature tables
   (`_PROVEN_COMPOSER_NEWLINE`, `_PROVEN_STEER_CHORDS`,
   `_RENDERED_SESSION_PROVEN_BUILDS`) if the build passes.  Only then return
   the provider to `open`.

Do not leave a provider in strict mode indefinitely without updating the pin
tables and filing the regression fix: that would reintroduce the stale-pin
breakers the open policy exists to remove.

## Adding a proven build

When a new provider build has been verified:

1. Add it to the provider's `SUPPORTED_VERSIONS` tuple (current first).
2. Update that provider's `PINNED_VERSIONS` reference build.
3. Add separate proven entries to the provider's feature tables.
4. Add a separate `RenderedSessionProof` entry to
   rendered-session proof table if the build's native header and process
   identity were actually verified.
5. Keep that provider's enforcement mode `open` unless you are deliberately
   reverting to strict.

For a provider temporarily held strict, only steps 1 and 2 are needed before
the exact build can launch. Return it to open after the compatibility fix is
merged and deployed.

## Narrow capability tables: the Codex native-bind exception

The Codex launch paths capture their pre-task harness-native session id
through a zero-turn app-server bootstrap (`thread/start` +
`thread/name/set` with no `turn/*`) so the resumed TUI can guarantee an exact,
resumable session before any task byte reaches the pane.  That guarantee is a
capability claim about the installed build: the full exchange
(`initialize -> initialized -> config/read -> thread/start -> thread/name/set ->
clean process exit`, canonical UUID, exact cwd/model/effort, one materialized
rollout, fresh `thread/resume` adopting the same id) must have been
stage-verified for the exact binary.  Builds proven for that contract are
listed in `provider_contracts.NATIVE_BIND_CAPABLE_VERSIONS` — currently
`0.146.0` and `0.147.0` — and `codex_native_bootstrap.BOOTSTRAP_CAPABLE_VERSIONS`
is that same table's Codex cell, so the bootstrap that mints a native id and
the managed bind seam that accepts it cannot disagree about which builds are
proven.

This is deliberately a NARROW exception table, independent of the launch-mode
policy **and** of the broad `SUPPORTED_VERSIONS` table:

- **Open launch mode does NOT carry exact-session capture.** A build accepted
  at the launch identity boundary may still be unproven for the bootstrap
  contract.  The Codex launch paths keep their fail-closed capability
  boundary: an installed build outside the table cannot supply the pre-task
  identity contract, and the launch fails closed with a typed refusal — zero
  provider initialization, zero task bytes — rather than silently degrading
  to a launch that cannot resume its own session.
- **Native bind accepts exactly the proven builds, no more.** The managed-v2
  bind seam (`managed_launch_v2._validate_readiness_for_bind`) asks its
  version question through `provider_contracts.is_native_bind_capable`, so a
  stage-proven build (0.147.0) binds while an unproven one (0.148.0) is
  refused with zero task bytes even though open launch policy admitted it.
  The seam must never consult the broad table instead: doing so reproduced a
  real forward-compatibility failure where a 0.147.0 native launch completed
  the bootstrap, exposed its exact session identity, reported `input_ready`,
  and was then refused at bind.
- **Bind capability grants no advanced authority.** The narrow table is not a
  step toward the broad one: a bound 0.147.0 generation still gets no
  composer/control/steer authority (those tables stay pinned to their own
  stage-verified builds), no resume/recovery identity, and no route-receipt
  authority, until each surface is independently proven and the broad table
  is updated through the full "Adding a proven build" procedure above.
- **Unsupported builds do not retain exact-session capture.**  Do not
  configure, document, or operate a Codex launch as if an unproven semver
  could mint a resumable id; it cannot, by design.

For the other providers the native-bind cell is their `SUPPORTED_VERSIONS`
tuple by reference — their native identity paths were verified with each
accepted build — so their bind behaviour is exactly their broad proven set,
and a future narrow exception for them is written as its own literal cell
here rather than by narrowing the broad table.

**Operator remediation:** install (or pin) a Codex build that is in
`NATIVE_BIND_CAPABLE_VERSIONS`; if a newer build must serve the launch path,
stage-verify it against the exact bootstrap contract above, add it to the
table, and re-verify the resumed-TUI surface before removing any version
override.  While the installed build is unproven, Codex launches refuse
fail-closed at the bootstrap, and a managed generation that somehow reached
bind with an unproven build is refused there too.

## Muse profile-carrier exception

Muse's managed native profile uses an internal file environment surface. Its
authority is narrower than Muse's semver-level resume support: it is enabled
only when the runtime observes the exact full banner `Muse Code 0.1.0
(0.1.0-R708.1)` and the resolved inner `muse-bin-*` executable has the
stage-proven SHA-256 recorded in the closed profile-carrier cell. The
update-capable `muse` launcher script is never that evidence. A same-semver R
revision or changed inner digest advertises and launches as
`profile_carrier_unverified` until separately stage-verified.

## Fail-closed invariants

These hold regardless of mode:

* An unknown provider name raises `ProviderContractError`.
* An unparseable version banner raises `ProviderVersionDrift`.
* A version not in `SUPPORTED_VERSIONS` gets no native control, no
  rendered-session proof, no steer/composer authority, no image authority,
  no ACP resume identity, and no route-receipt authority.
* A version not in `NATIVE_BIND_CAPABLE_VERSIONS` cannot become a managed
  generation's bound native identity, whatever the launch mode admitted;
  membership there grants bind/admission only — never any of the advanced
  authorities above, which stay governed by `SUPPORTED_VERSIONS` and the
  per-feature tables.
* `IMAGE_PROVEN_BUILDS` stays pinned to the builds that actually demonstrated
  image delivery; adding a build to `SUPPORTED_VERSIONS` does not grant it
  image authority.
