# CAO Documentation Site

This directory contains the documentation website for CLI Agent Orchestrator, built with [Docusaurus](https://docusaurus.io/).

The site is deployed to https://awslabs.github.io/cli-agent-orchestrator/ via GitHub Pages.

## Local Development

```bash
cd docusaurus
npm install
npm run start
```

This starts a local development server at `http://localhost:3000/cli-agent-orchestrator/` with hot reloading.

## Build

```bash
npm run build
```

This generates static content into the `build` directory.

## Dependency security

Use npm 10+ and `npm ci` to install the committed dependency graph and apply its local
security patch. Do not disable install scripts: `postinstall` runs
`patch-package --patch-dir ../patches --error-on-fail`. `npm run build` runs
`npm run test:dependencies` before assembling the courses and site; the same
checks can be run independently while updating dependencies.

Relevant dependency security controls include:

- **`braces`** resolves through the private local `braces-compat` adapter to the
  canonical published third-party MIT fork
  `@dieub/braces-depth-guard@3.0.3-pn.3`, shared with the web UI and
  MCP Apps toolchains. The unchanged fork tarball is
  [vendored in the repository](../vendor/README.md), so a cold install does not
  depend on its registry availability. The adapter only re-exports the fork;
  keep `.npmrc`'s `install-links=false` setting and include `braces-compat/`
  and `../vendor/` when installing the project. It replaces the affected original while retaining
  quoted/escaped pattern behavior, and bounds parsing and recursive AST depth
  to 100 (or a lower `maxDepth`). Both `chokidar` and `micromatch` resolve the
  fork under their existing `braces` import.
  [`../patches/@dieub+braces-depth-guard+3.0.3-pn.3.patch`](../patches/@dieub+braces-depth-guard+3.0.3-pn.3.patch)
  retains our additional bound on acyclic ancestor traversal; the fork already
  rejects parent cycles. This is not an official fixed upstream release or a
  scanner exemption. See [SECURITY.md](../SECURITY.md#local-dependency-mitigations)
  for source/provenance verification, the exact-version requirement, and
  conditions for returning to an official release.
- **`http-cache-semantics`** requires `^4.3.0`, outside the affected range
  currently recorded for
  [GHSA-ch52-4w7c-c8xp](https://github.com/advisories/GHSA-ch52-4w7c-c8xp).
  The advisory's `max-stale` interpretation is
  [disputed by the maintainer](https://github.com/kornelski/http-cache-semantics/issues/56#issuecomment-5975759591);
  this upgrade does not claim to change that behavior. Checks preserve
  non-storage of private/no-store responses and permitted reuse of stale
  public responses, and cover the release's stricter wildcard `Vary` handling.
- **`postcss-selector-parser`** requires `^7.1.6` for both the v6 and v7
  consumer ranges. The v6 line has no fixed release for CVE-2026-104844.
  Selector serialization/class traversal regressions and the full site build
  check compatibility with the existing PostCSS plugins.
- **`shell-quote`** requires `^1.11.0` to fix
  [GHSA-pqg4-j6r4-53mv](https://github.com/advisories/GHSA-pqg4-j6r4-53mv)
  (CVE-2026-102422). It rejects line terminators in arguments after a comment
  token. Tests exercise the dependency resolved by `launch-editor`, including
  parsed comments and non-adjacent arguments, while preserving ordinary quoting
  and multiline arguments before a comment.
- **`tinypool`** is pinned to `2.1.2`, which fixes CVE-2026-104848 and
  CVE-2026-104849. Docusaurus still requests v1; v2 supports Node 20 and Node
  22+, matching the tested site toolchains. The dependency suite
  exercises worker creation, task execution, cleanup, and the worker-data/state
  layout consumed by Docusaurus. Keep this override until Docusaurus selects a
  fixed version itself.

The lockfile also includes upstream security releases for `source-map-js`
(`1.2.2`), `compression` (`1.8.2`), `proxy-addr` (`2.0.8`), and `joi`
(`17.13.8`). Source-map regressions cover ordinary mappings, invalid offsets,
and direct/nested aggregate line limits. Update every affected toolchain rather
than only the lockfile named by an individual Dependabot PR.

These dependencies belong to the documentation build/development toolchain;
GitHub Pages serves the resulting static site. No alert is suppressed.

The repository's [Dependency Security gate](../SECURITY.md#full-dependency-gate)
audits the complete locked graphs on every PR/`main` CI run and weekly. It
reports installed mitigation verification separately from open advisories.
Changes to the vendored archive, shared patch, or brace tests also trigger the docs site build.

## Adding Documentation

1. Add markdown files to `docs/` following the existing directory structure
2. Update `sidebars.ts` if adding new pages
3. Run `npm run build` to verify there are no broken links
4. Submit a PR — the site auto-deploys when changes merge to `main`

## Adding a blog post

Posts live in `blog/`, one directory per post
(`blog/YYYY-MM-DD-short-slug/index.md`). Authors and tags are registries rather
than free-form front matter: the build fails on an author missing from
`blog/authors.yml`, a tag missing from `blog/tags.yml`, or a post with no
`{/* truncate */}` marker.

See [Writing a blog post](../CONTRIBUTING.md#writing-a-blog-post) for the full
process and style rules.


## Deploying on a fork

The `Docs site` workflow (`.github/workflows/gh-pages.yml`) always builds the
site on pull requests and pushes to `main` so you get a build signal, but by
default it only **deploys** to GitHub Pages on the upstream repo
(`awslabs/cli-agent-orchestrator`). On a fork, the `deploy` job is skipped —
this avoids the workflow failing with `Error: Failed to create deployment
... 404` (`actions/deploy-pages` errors because GitHub Pages isn't enabled on
most forks).

If you maintain a fork and want it to publish its own docs site, opt in
explicitly:

1. In your fork, go to **Settings → Pages** and set **Source** to **GitHub
   Actions**.
2. Go to **Settings → Secrets and variables → Actions → Variables** and add a
   repository variable named `DEPLOY_DOCS_PAGES` with the value `true`.
3. Push to `main` (or re-run the workflow). The `deploy` job will now run and
   publish to `https://<your-username>.github.io/cli-agent-orchestrator/`.

Without both of these steps, leave `DEPLOY_DOCS_PAGES` unset (or `false`) so
the deploy job stays disabled and the workflow doesn't fail on your fork.

## Directory Structure

```
docusaurus/
├── docs/                  # Markdown documentation content
│   ├── intro.md
│   ├── getting-started/
│   ├── core-concepts/
│   ├── patterns/
│   ├── features/
│   ├── guides/
│   └── reference/
├── blog/                  # Blog posts, one directory per post
│   ├── authors.yml        # Registry of blog authors (enforced at build time)
│   └── tags.yml           # Registry of blog tags (enforced at build time)
├── course-src/            # Interactive course sources (assembled into static/)
│   ├── build.sh           # Concatenates each course into a single page
│   ├── shared/            # styles.css + main.js shared by both courses
│   ├── fundamentals/      # _base.html, _footer.html, modules/
│   └── advanced/          # _base.html, _footer.html, modules/
├── src/                   # Custom React components and pages
├── static/                # Static assets, plus the generated courses
├── docusaurus.config.ts   # Main site configuration
└── sidebars.ts            # Sidebar navigation structure
```

## Interactive Courses

The two courses under `course-src/` are plain HTML assembled by
`course-src/build.sh` into `static/course/`, `static/course-advanced/`, and
`static/course-assets/`. That output is gitignored and rebuilt automatically by
the `prebuild` and `prestart` npm scripts, so edit the sources in `course-src/`
and never the assembled pages.

To rebuild them without a full site build:

```bash
npm run build-courses
```

## Brand Assets

`static/img/` holds the logo, and `cao-mark.svg` is the canonical geometry:

| File | Used by |
| --- | --- |
| `cao-mark.svg` | navbar in light mode |
| `cao-mark-dark.svg` | navbar in dark mode |
| `favicon.svg` | source for `favicon.ico` |
| `favicon.ico` | `favicon` in `docusaurus.config.ts`, and the web dashboard |
| `cao-social-card.svg` | source for the card below |
| `cao-social-card.png` | `themeConfig.image`, the GitHub repo social preview, and the banner at the top of both root READMEs |
| `cao-lockup.svg` | source for the light lockup PNG |
| `cao-lockup-dark.svg` | source for the dark lockup PNG |
| `cao-lockup.png` / `cao-lockup-dark.png` | the homepage hero heading in `src/pages/index.tsx`, swapped by `ThemedImage` |

Two variants exist because dark mode paints the navbar pure black (see the
navbar background override in `src/css/custom.css`) and the mark's navy
contrasts against that at roughly 2:1, under the 3:1 WCAG floor for graphical
objects. The dark file lifts navy to a light tint and changes nothing else.
`favicon.svg` is a third drawing: a solid hexagon with the graph knocked out in
white, because the outlined mark turns to mush at 16px and a favicon gets no
say in what background it lands on.

The card is committed as a PNG because neither GitHub's social preview nor
`og:image` consumers accept SVG. The root READMEs use it as their banner, where
it earns its keep by supplying its own background: the bare mark would need a
per-theme swap, and it cannot have one, because README.md doubles as the PyPI
long description and PyPI disallows `<picture>`.

That same constraint applies to the card. It is light, which matches PyPI, the
official artwork, and GitHub's light theme, and it shows as a bright panel on
GitHub's dark theme. It carries the name and tagline only, not the repo URL: as
a README banner the URL would sit on the page it points at, and in an unfurl the
link is already the thing being unfurled. Regenerate the card after editing the
source — the text baselines are measured against the mark's ink box, so re-check
the centering if you change the type sizes or the mark's transform:

```bash
cd static/img
rsvg-convert -w 1280 -h 640 cao-social-card.svg -o cao-social-card.png
```

`favicon.ico` is a three-size container (16/32/48) packed from `favicon.svg`,
which `rsvg-convert` alone cannot write. Rebuild it from the repository root
with:

```bash
python3 scripts/build_favicon.py
```

That script also writes `web/public/favicon.ico` and `web/public/favicon.svg`.
The web dashboard needs its own copies because Vite publishes only what is under
`web/public/` and Docusaurus only what is under `docusaurus/static/`, so neither
can point at the other's file. Run the script rather than editing either copy by
hand, so the two cannot drift.

The lockup pairs the mark with the two-line wordmark and is the only asset that
shows the logo in full. It ships as a PNG for the same reason the card does, but
for a sharper reason: the wordmark is live `<text>`, so an SVG on the page would
render in whatever font each *visitor* happens to have, and the logo would
differ per machine. Rasterizing at author time pins it.

### The brand typeface

The wordmark is **Inter Bold**, in both the lockup and the social card. Inter is
not a system font on any platform, so it has to be installed before you can
regenerate any asset containing text. Without it `rsvg-convert` silently falls
back to Arial and writes a file that looks wrong in a way no error reports.

Inter is under the SIL Open Font License. Install it system-wide, or scope it to
just the one command:

```bash
curl -sLo /tmp/inter.zip https://github.com/rsms/inter/releases/download/v4.1/Inter-4.1.zip
mkdir -p /tmp/interfonts/fonts
unzip -jo /tmp/inter.zip 'extras/ttf/Inter-Bold.ttf' 'extras/ttf/Inter-Regular.ttf' \
  -d /tmp/interfonts/fonts
export XDG_DATA_HOME=/tmp/interfonts     # fontconfig reads $XDG_DATA_HOME/fonts
fc-match Inter:bold                      # must print Inter-Bold.ttf, not a fallback
```

Then regenerate both lockup variants together:

```bash
cd static/img
rsvg-convert -w 1800 -h 480 cao-lockup.svg -o cao-lockup.png
rsvg-convert -w 1800 -h 480 cao-lockup-dark.svg -o cao-lockup-dark.png
```

1800px wide covers the 600px display width at 3x. The viewBox is trimmed to the
artwork's ink box so the hero can center it without phantom margin.

The typeface is now correct, but the artwork around it is still a redraw from a
raster reference. The hexagon's interior graph and the exact brand hex values
are the parts to check against the official vector file, and the whole set
should be replaced by it rather than patched toward it.
