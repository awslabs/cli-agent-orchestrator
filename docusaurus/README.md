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
├── src/                   # Custom React components and pages
├── static/                # Static assets (images, favicon)
├── docusaurus.config.ts   # Main site configuration
└── sidebars.ts            # Sidebar navigation structure
```
