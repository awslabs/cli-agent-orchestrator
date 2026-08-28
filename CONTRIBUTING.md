# Contributing Guidelines

Thank you for your interest in contributing to our project. Whether it's a bug report, new feature, correction, or additional
documentation, we greatly value feedback and contributions from our community.

Please read through this document before submitting any issues or pull requests to ensure we have all the necessary
information to effectively respond to your bug report or contribution.


## Reporting Bugs/Feature Requests

We welcome you to use the GitHub issue tracker to report bugs or suggest features.

When filing an issue, please check existing open, or recently closed, issues to make sure somebody else hasn't already
reported the issue. Please try to include as much information as you can. Details like these are incredibly useful:

* A reproducible test case or series of steps
* The version of our code being used
* Any modifications you've made relevant to the bug
* Anything unusual about your environment or deployment


## Contributing via Pull Requests
Contributions via pull requests are much appreciated. Before sending us a pull request, please ensure that:

1. You are working against the latest source on the *main* branch.
2. You check existing open, and recently merged, pull requests to make sure someone else hasn't addressed the problem already.
3. You open an issue to discuss any significant work - we would hate for your time to be wasted.

To send us a pull request, please:

1. Fork the repository.
2. Modify the source; please focus on the specific change you are contributing. If you also reformat all the code, it will be hard for us to focus on your change.
3. Ensure local tests pass.
4. Commit to your fork using clear commit messages.
5. Send us a pull request, answering any default questions in the pull request interface.
6. Pay attention to any automated CI failures reported in the pull request, and stay involved in the conversation.

GitHub provides additional document on [forking a repository](https://help.github.com/articles/fork-a-repo/) and
[creating a pull request](https://help.github.com/articles/creating-a-pull-request/).


## Developing in GitHub Codespaces

The project runs end-to-end inside a Codespace. See [docs/codespaces.md](docs/codespaces.md)
for how to start `cao-server`, forward port `9889`, and troubleshoot 404s on the
forwarded URL.


## Recording test fixtures safely

Many provider tests use fixtures captured from **live CLI output** (see
`test/providers/fixtures/`). That output can embed personal or sensitive data —
notably login/banner lines that print your account email, and tokens that hide
inside ANSI escape sequences where they are easy to miss in review. A real
incident (#436) merged personal emails this way.

When recording or updating a live-output fixture:

- **Capture on a synthetic/throwaway account** where possible, not your personal
  or corporate account.
- **Scrub any login/banner/identity line before committing** — replace a real
  email with `user@example.com`, an account name with a placeholder, etc.
- **Skim the raw bytes, not just the rendered view.** A secret can sit next to
  ANSI escape codes and not be visible in a normal terminal render or diff.
- Prefer the reserved placeholders scanners already treat as safe:
  `user@example.com`, and for AWS the documented `AKIAIOSFODNN7EXAMPLE`.

A gitleaks secret scan runs on every PR and weekly over full history (see
[SECURITY.md](SECURITY.md#secret-scanning)); you can run it locally with
`scripts/security-scan.sh gitleaks`, or enable the optional pre-commit hook
([`.pre-commit-config.yaml`](.pre-commit-config.yaml)) with `pre-commit install`
to catch secrets before they enter a commit. If a secret ever lands, follow the
[Leak Response & Git-History Scrub Runbook](docs/security.md)
— **rotate/revoke first**, rewrite history only if warranted by the runbook's
severity triage table.


## Writing a blog post

The documentation site has a [blog](https://awslabs.github.io/cli-agent-orchestrator/blog)
for content that doesn't fit the reference docs: tutorials, design deep dives,
and accounts of using CAO on real work. Posts live in `docusaurus/blog/` and go
through normal pull request review.

**In scope:** tutorials and how-tos, deep dives on how a subsystem works,
orchestration patterns composed into something real, provider or MCP integration
write-ups, and case studies — including the parts that didn't work.

**Out of scope:** release announcements (`CHANGELOG.md` and GitHub Releases
already cover those, and we deliberately don't mirror them here) and product
marketing.

**Editorial review:** [@haofeif](https://github.com/haofeif) (Haofei Feng) owns
editorial review for the blog — request their review on your PR. The build
enforces structure, but whether a post is worth publishing, and whether it reads
well, is their call. If you're unsure an idea fits, ask in a
[discussion](https://github.com/awslabs/cli-agent-orchestrator/discussions)
before you write; that's a cheaper conversation than a rejected draft.

To add a post:

1. Create `docusaurus/blog/YYYY-MM-DD-short-slug/index.md`. Use a *directory*
   rather than a bare `.md` file so images sit next to the post and can be
   referenced relatively (`![alt](./diagram.png)`).
2. Add front matter — `title`, `authors`, and optionally `tags`, `slug`,
   `description`, `image`:

   ```yaml
   ---
   title: Orchestrating a three-agent review pipeline
   authors: [your-github-handle]
   tags: [tutorial, orchestration-patterns]
   description: One sentence for search results and social previews.
   ---
   ```

3. Make sure you have an entry in [`docusaurus/blog/authors.yml`](docusaurus/blog/authors.yml)
   — several contributors are already seeded there from public GitHub profile
   data, so check before adding a duplicate, and correct your own name, title, or
   avatar if the seeded values aren't what you'd want on a byline. The build
   **fails** on an author with no entry, so this isn't optional. That file's
   comments also cover the optional per-author listing page.
4. Pick tags from [`docusaurus/blog/tags.yml`](docusaurus/blog/tags.yml). The
   build **fails** on an unlisted tag; adding a new one is fine, just do it in
   the same PR and say why. A post with no tags is also fine.
5. Put `{/* truncate */}` after the first paragraph or two. Everything above it
   becomes the summary on the blog index, and it must stand on its own. The build
   **fails** without it. Use that MDX comment form, not the HTML `<!-- -->` one —
   this site processes `.md` as MDX, where HTML comments are a syntax error.
6. Verify locally before pushing:

   ```bash
   cd docusaurus
   npm install
   npm run build   # onBrokenLinks: 'throw' — a dead link fails the build
   npm run serve   # preview the built site
   ```

Style notes, mostly borrowed from how other AWS open source projects run their
blogs:

* Write for a global audience — skip regional slang and idioms.
* Avoid "easy" and "simple"; describe what a step actually involves instead.
* Avoid absolute claims ("always", "never", "guarantees") and comparisons against
  other products.
* Run every command and code sample against a released version before you publish
  it, and say which version you used.
* Link to documentation pages by site route — `[handoff](/docs/patterns/handoff)`,
  not a relative `../../docs/patterns/handoff.md` path, which Docusaurus can't
  resolve from a blog post. `scripts/validate_markdown_links.py` skips
  `docusaurus/blog/` for that reason; the site build is what checks these links.
* Give images alt text, and don't include screenshots containing personal data —
  the same scrubbing rules as test fixtures apply.

Your post is contributed under the repository's Apache-2.0 license, like any
other change.


## Finding contributions to work on
Looking at the existing issues is a great way to find something to contribute on. As our projects, by default, use the default GitHub issue labels (enhancement/bug/duplicate/help wanted/invalid/question/wontfix), looking at any 'help wanted' issues is a great place to start.


## Code of Conduct
This project has adopted the [Amazon Open Source Code of Conduct](https://aws.github.io/code-of-conduct).
For more information see the [Code of Conduct FAQ](https://aws.github.io/code-of-conduct-faq) or contact
opensource-codeofconduct@amazon.com with any additional questions or comments.


## Security issue notifications
If you discover a potential security issue in this project we ask that you notify AWS/Amazon Security via our [vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/). Please do **not** create a public github issue.


## Licensing

See the [LICENSE](LICENSE) file for our project's licensing. We will ask you to confirm the licensing of your contribution.
