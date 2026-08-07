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

## Fail-closed invariants

These hold regardless of mode:

* An unknown provider name raises `ProviderContractError`.
* An unparseable version banner raises `ProviderVersionDrift`.
* A version not in `SUPPORTED_VERSIONS` gets no native control, no
  rendered-session proof, no steer/composer authority, no image authority,
  no ACP resume identity, and no route-receipt authority.
* `IMAGE_PROVEN_BUILDS` stays pinned to the builds that actually demonstrated
  image delivery; adding a build to `SUPPORTED_VERSIONS` does not grant it
  image authority.
