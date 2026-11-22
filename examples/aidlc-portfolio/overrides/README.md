# Portfolio provider override

Keeps the example skill and TypeScript sources unchanged. Builds isolated Claude
and Codex copies plus a wrapper that reads the installed `aidlc_runner.md` on
every invocation. `claude_code` selects the original runtime; `codex` selects the
Codex projection. Explicit conflicting `--provider` values fail.

From the example directory, install dependencies with `bun install --frozen-lockfile`,
then run:

```sh
bun overrides/deploy.ts
```

Installation adds `~/.aws/cli-agent-orchestrator/skills/aidlc-portfolio-override`
and appends a precedence directive to the two installed profiles and their
contexts. Original profiles are backed up once with `.before-provider-override`.
Restart the supervisor so it loads the directive. The original skill is preserved.

Alternatively build without deployment:

```sh
bun overrides/build.ts /tmp/aidlc-portfolio-override
bun /tmp/aidlc-portfolio-override/scripts/portfolio.ts help
```

`AIDLC_RUNNER_PROFILE` selects an alternative profile for testing. There is no
fallback when the profile is missing or unsupported. Upstream source anchors
are checked at build time, so incompatible upstream changes require reviewing
the override rather than silently retaining Claude-only gates.

Codex distributions must contain `.codex/config.toml`,
`.agents/skills/aidlc/SKILL.md`, and `aidlc/`. Both runtime directories and the
AI-DLC data are manifest hashed, transactionally projected, and checked before
dispatch. Existing tracked paths are refused. Model selection remains in the
CAO runner profile; no Anthropic settings are written. This manages distribution
files, not project onboarding or Codex trust. The inspected Codex copy runtime
has no root `AGENTS.md`; existing project instructions are preserved.

To roll back, restore both installed profiles and contexts from the backups and
restart the supervisor. Do not mix providers inside one active portfolio: a
provider switch requires staging and syncing the corresponding distribution.
