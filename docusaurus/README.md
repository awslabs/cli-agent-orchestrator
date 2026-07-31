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

## Adding Documentation

1. Add markdown files to `docs/` following the existing directory structure
2. Update `sidebars.ts` if adding new pages
3. Run `npm run build` to verify there are no broken links
4. Submit a PR — the site auto-deploys when changes merge to `main`

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
