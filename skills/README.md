# Skills

Reusable skill definitions for AI coding agents. Each skill provides specialized knowledge, workflows, and checklists that agents can follow. Skills follow the [Agent Skills](https://agentskills.io) open standard.

This is the **single source of truth** for all skills. Tool-specific directories are local copies that should not be tracked in git.

## Available Skills

| Skill | Description |
|-------|-------------|
| `build-cao-provider` | Full lifecycle guide for building a new CLI agent provider — terminal output capture, implementation, unit/E2E tests, security, documentation |
| `skill-creator` | Guide for creating new skills with proper structure, references, and output patterns |

## Installing Skills

Each AI coding tool reads skills from its own directory:

| Tool | Skills Directory | Reference |
|------|-----------------|-----------|
| Claude Code | `.claude/skills/` | [docs](https://docs.claude.ai) |
| Codex CLI | `.agents/skills/` | [docs](https://developers.openai.com/codex/skills/) |
| Gemini CLI | `.gemini/skills/` | [docs](https://geminicli.com/docs/cli/skills/) |
| Kimi CLI | `.kimi/skills/` | Also checks `.agents/skills/` |
| Kiro | `.kiro/skills/` | [docs](https://kiro.dev/docs/cli/custom-agents/configuration-reference/#skill-resources) |

### Per tool

```bash
# Claude Code
cp -r skills/ .claude/skills/

# Codex CLI
mkdir -p .agents/skills
cp -r skills/* .agents/skills/

# Gemini CLI
mkdir -p .gemini/skills
cp -r skills/* .gemini/skills/

# Kimi CLI
mkdir -p .kimi/skills
cp -r skills/* .kimi/skills/

# Kiro
mkdir -p .kiro/skills
cp -r skills/* .kiro/skills/
```

### All tools at once

```bash
for tool in .claude .agents .gemini .kimi .kiro; do
  mkdir -p "$tool/skills"
  cp -r skills/* "$tool/skills/"
done
```

### Ralph (autonomous verification)

Ralph reads reference files from `.ralph/specs/` and uses its own config files. Copy the references and templates separately:

```bash
mkdir -p .ralph/specs
cp skills/build-cao-provider/references/*.md .ralph/specs/
cp skills/build-cao-provider/templates/ralph/* .ralph/
```

### Everything (all tools + Ralph)

```bash
for tool in .claude .agents .gemini .kimi .kiro; do
  mkdir -p "$tool/skills"
  cp -r skills/* "$tool/skills/"
done

mkdir -p .ralph/specs
cp skills/build-cao-provider/references/*.md .ralph/specs/
cp skills/build-cao-provider/templates/ralph/* .ralph/
```

## Updating Skills

When a skill is modified in `skills/`, re-run the install commands above to sync changes. The tool-specific directories are in `.gitignore` so only `skills/` is tracked in git.

## Structure

```
skills/
├── README.md
├── build-cao-provider/
│   ├── SKILL.md                          # Main skill definition
│   ├── references/
│   │   ├── implementation-checklist.md   # File-by-file creation guide
│   │   ├── lessons-learned.md            # Critical bugs and fixes from past providers
│   │   └── verification-checklist.md     # Testing, security, and documentation checks
│   └── templates/
│       └── ralph/                        # Ralph autonomous verification templates
│           ├── AGENT.md
│           ├── PROMPT.md
│           ├── fix_plan.md
│           └── ralphrc
└── skill-creator/
    ├── SKILL.md
    ├── references/
    │   ├── output-patterns.md
    │   └── workflows.md
    └── scripts/
        ├── init_skill.py
        ├── package_skill.py
        └── quick_validate.py
```
