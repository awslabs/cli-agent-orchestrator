---
slug: welcome
title: Welcome to the CAO blog
authors: [cao-maintainers]
tags: [community]
description: Why this blog exists, what belongs here, and how to publish a post.
---

The reference documentation tells you what CAO does. It's a poor fit for the
other half of the story — the walkthrough that takes an hour, the design
decision that needs three diagrams to justify, the write-up of an orchestration
that worked on real work and the two that didn't. This blog is where that goes.

Anyone who contributes to CLI Agent Orchestrator can publish here. Posts live in
`docusaurus/blog/` and ship through the same pull request review as the rest of
the repository.

{/* truncate */}

## What belongs here

- **Tutorials and how-tos** — a task followed end to end, with commands a reader
  can run.
- **Deep dives** — how a subsystem works and why it's built that way. Sessions,
  the MCP server, and provider adapters all have more depth than their reference
  pages carry.
- **Orchestration patterns in practice** — [handoff](/docs/patterns/handoff),
  [assign](/docs/patterns/assign), and
  [send-message](/docs/patterns/send-message) composed into something real.
- **Case studies** — what you built, what it cost, and where it fell over. The
  failures are the useful part.

## What doesn't

**Release announcements.** [`CHANGELOG.md`][changelog] and
[GitHub Releases][releases] already carry them, and a blog that mirrors a
changelog becomes a stale changelog. We're deliberately not doing that.

**Marketing.** No product pitches, no competitor comparisons, no claims a reader
can't verify from the repository.

## Publishing a post

The [contributing guide][contributing] has the mechanics: the file layout, the
front matter, registering yourself in `blog/authors.yml`, and the style rules the
build enforces. The short version is that a post is a directory, a Markdown
file, and a pull request.

If you're not sure whether an idea fits, open a
[discussion][discussions] and ask before you write. It's a cheaper conversation
than a rejected draft.

[changelog]: https://github.com/awslabs/cli-agent-orchestrator/blob/main/CHANGELOG.md
[releases]: https://github.com/awslabs/cli-agent-orchestrator/releases
[contributing]: https://github.com/awslabs/cli-agent-orchestrator/blob/main/CONTRIBUTING.md#writing-a-blog-post
[discussions]: https://github.com/awslabs/cli-agent-orchestrator/discussions
