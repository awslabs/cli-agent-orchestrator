# COND-0323 Kimi workspace-trust evidence

This note records the provider-contract check behind the native-TUI
preflight change. It is version-sensitive evidence, not a replacement for
the exact provider-version admission table.

## Contract observed

In the Kimi Code `agent-core-v2` bundle, `WorkspaceTrustService` stores one
JSON document under the `workspace-trust` scope. Its key is the v2
`encodeWorkDirKey(realpath(cwd))` value:

```
wd_<lowercase-basename-slug-up-to-40>_<sha256(realpath-cwd)[:12]>
```

The trusted document contains the workspace root and a positive millisecond
timestamp:

```json
{"root":"/private/tmp/example-worktree","trustedAt":1730000000000}
```

The managed launcher writes this provider-owned record in the generation's
private `KIMI_CODE_HOME` before creating the native pane, then reads it back.
It never types through the provider's trust prompt.

## Reproduction and live check

Using a disposable worktree and private `KIMI_CODE_HOME` on 2026-08-06:

1. Launching the provider with no trust document displayed `Trust this
   folder?` before the Welcome TUI and before any session/model input.
2. Writing the exact key and record above, with the root normalized to the
   provider's `/private/tmp/...` realpath, reached the Welcome TUI directly;
   no trust interstitial was rendered.

The check was repeated against these exact bundles:

| Kimi version | `dist/main.mjs` SHA-256 | result |
| --- | --- | --- |
| 0.33.0 | `0e77b9c64e67a4eecb96aae011750668aab11bd781564fe3e4855513812247b2` | trust record accepted |
| 0.34.0 | `d3e781774e7a95f71e9d813e2cda95486d15db73712b3e821dd4a357b0511d8c` | trust record accepted |

The 0.34.0 observation does **not** widen CAO's accepted provider-version
set. Version admission remains the separate fail-closed contract in
`services/provider_contracts.py`; a route using an unaccepted version is
blocked before this trust preflight is relevant.

