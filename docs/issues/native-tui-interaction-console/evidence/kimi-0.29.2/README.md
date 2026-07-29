# Kimi 0.29.2 staged-path image proof (sanitized, portable)

Live acceptance executed 2026-07-28 (round `native-tui-console/spec-kimi-staged-path-r3`)
substantiating design.md §8.2 / §8.6 / §10.6 / OD7. Full raw artifacts remain uncommitted
under `.conductor/reports/r3/`; this directory is the sanitized, reviewable subset.

## What is proven

A disposable native Kimi Code **0.29.2** session (model K3, session
`session_4eea95f9-2873-4be9-920c-24566f6e37f6`, private tmux fixture, default home, no CAO
production code) was handed ordinary composer text naming the absolute staged path of
`stripe-fixture.png`. The transcript shows the provider invoking its own `ReadMediaFile`
tool on that exact file (`image (image/png, 213 B)`) and reporting the known visual fact
correctly: **red on the left half, blue on the right half** — matching the fixture by
construction.

## Files

- `stripe-fixture.png` — 120×80 px, 8-bit RGB, non-interlaced, 213 bytes; left half
  `(255,0,0)`, right half `(0,0,255)` (Python stdlib struct+zlib, deterministic).
  SHA-256: `02f597dc736f7dddcf8710cdc791fb8ff06025fff98304c86b972f14e87bdd49`.
- `transcript-excerpt.txt` — verbatim excerpt: the exact prompt (with absolute staged
  path), the `Used ReadMediaFile … (image/png, 213 B)` line, the correct observation,
  and the disposable session's own `/status` identity lines (version/model/directory/
  session). Unrelated transcript content (MCP banners, harness chatter) removed; no
  credentials present. SHA-256: `9ac5fee55dc8d1768b450db56fc388198e5028aa3ef5199bc6052dc05b1f210f`.

## Scope of the claim

- **Proven:** staged absolute PNG path delivered as ordinary composer text → provider
  `ReadMediaFile` reads and correctly observes it, on the pinned 0.29.2 build.
- **Not proven (refused in the spec):** clipboard-paste image delivery into the kimi TUI;
  non-PNG formats; bare-path (non-directive) substitution as the trigger — the proven
  prompt used the explicit `ReadMediaFile` directive form, which is what §8.6 pins as
  kimi's `reference_template`.
