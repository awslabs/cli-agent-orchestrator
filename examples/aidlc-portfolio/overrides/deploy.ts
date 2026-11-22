import { readFile, writeFile, copyFile } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import { build } from './build.ts';
const home = process.env.HOME!;
const base = resolve(home, '.aws/cli-agent-orchestrator');
const target = join(base, 'skills/aidlc-portfolio-override');
await build(target);
const marker = '\n\n## Installed provider override\n';
const directive = `${marker}Load aidlc-portfolio-override instead of aidlc-portfolio. For ALL portfolio commands set SKILL_DIR to ${target} and invoke its scripts/portfolio.ts. This instruction supersedes earlier Claude-only distribution and Opus overlay instructions. The wrapper reads agent-store/aidlc_runner.md on every invocation; its provider selects the harness for both dispatch and phase gates. When provider is codex, use a Codex distribution with .codex/config.toml, .agents/skills/aidlc/SKILL.md, and aidlc/. Find the local distribution or request its location if unavailable. Do not use a Claude distribution for a Codex runner. Do not bypass validation, tracked-path protection, or human approval gates. Model selection belongs to the runner profile.\n`;
for (const directory of ['agent-store', 'agent-context']) {
 for (const name of ['aidlc_runner', 'aidlc_portfolio_supervisor']) {
  const path = join(base, directory, `${name}.md`);
  const content = await readFile(path, 'utf8');
  if (!content.includes(marker)) await copyFile(path, `${path}.before-provider-override`);
  await writeFile(path, content.split(marker)[0] + directive);
 }
}
console.log(`Installed ${target}`);
