import { cp, mkdir, readFile, writeFile } from 'node:fs/promises';
import { resolve, join } from 'node:path';

// Checked replacements deliberately fail on upstream drift rather than silently
// leaving a Claude-only gate in the generated Codex runtime.
function replace(source: string, from: string, to: string): string {
  if (!source.includes(from)) throw new Error(`Upstream override anchor missing: ${from}`);
  return source.replaceAll(from, to);
}
export function codexHarness(source: string): string {
  source = source.replaceAll('"claude"', '"codex"').replaceAll('.claude', '.codex');
  source = replace(source, 'join(".codex", "settings.json"),', 'join(".codex", "config.toml"),');
  source = replace(source, 'join(".codex", "skills", "aidlc", "SKILL.md"),', 'join(".agents", "skills", "aidlc", "SKILL.md"),');
  source = replace(source, '    ".codex",\n    "aidlc",', '    ".codex",\n    ".agents",\n    "aidlc",');
  source = replace(source, '  "/.codex/",', '  "/.codex/",\n  "/.agents/",');
  source = replace(source, 'const names = [".codex", "aidlc"];', 'const names = [".codex", ".agents", "aidlc"];');
  source = replace(source, '  await collectFiles(root, ".codex", files);', '  await collectFiles(root, ".codex", files);\n  await collectFiles(root, ".agents", files);');
  for (const origin of ['source', 'staged']) {
    const anchor = `    await cp(join(${origin}, "aidlc"),`;
    source = replace(source, anchor, `    await cp(join(${origin}, ".agents"), join(temporary, ".agents"), { recursive: true, errorOnExist: true });\n${anchor}`);
  }
  const start = source.indexOf('async function applyClaudeOverlay(');
  const end = source.indexOf('async function optionalDistributionFiles(', start);
  if (start < 0 || end < 0) throw new Error('Missing model overlay anchors');
  // The CAO runner profile owns the model. Never inject Anthropic settings or
  // rewrite the user's Codex configuration to select a model.
  source = source.slice(0, start) + `async function applyClaudeOverlay(_root: string, _model: string): Promise<void> {}\nasync function configuredOpusModel(_root: string): Promise<string> { return "runner-profile"; }\n\n` + source.slice(end);
  source = replace(source, 'opusModel = DEFAULT_OPUS_MODEL', 'opusModel = "runner-profile"');
  source = replace(source, '  const provider = requireProvider(providerInput);\n  const root = resolve(rootInput);\n  const source', '  const provider = requireProvider(providerInput);\n  opusModel = "runner-profile";\n  const root = resolve(rootInput);\n  const source');
  return source;
}

export async function build(destination: string, original = resolve(import.meta.dir, '../skills/aidlc-portfolio'), profile = resolve(process.env.HOME!, '.aws/cli-agent-orchestrator/agent-store/aidlc_runner.md')) {
  const target = resolve(destination);
  if (target === original || target.startsWith(original + '/')) throw new Error('Destination must be outside original skill');
  await cp(resolve(import.meta.dir, '../node_modules'), join(target, 'node_modules'), {
    recursive: true,
    filter: (path) => !path.split('/').includes('.bin'),
  });
  for (const provider of ['claude', 'codex']) {
    const directory = join(target, 'runtimes', provider);
    await cp(original, directory, { recursive: true });
    if (provider === 'codex') {
      const scripts = join(directory, 'scripts');
      await writeFile(join(scripts, 'harness.ts'), codexHarness(await readFile(join(scripts, 'harness.ts'), 'utf8')));
      let lib = await readFile(join(scripts, 'lib.ts'), 'utf8');
      lib = replace(lib, 'verifyHarness(root, "claude"', 'verifyHarness(root, "codex"');
      lib = replace(lib, '    ".codex/skills/aidlc/SKILL.md",', '    ".codex/skills/aidlc/SKILL.md",\n    ".agents/skills/aidlc/SKILL.md",');
      await writeFile(join(scripts, 'lib.ts'), lib);
      let cli = await readFile(join(scripts, 'portfolio.ts'), 'utf8');
      cli = replace(cli, 'options.provider ?? "claude"', 'options.provider ?? "codex"');
      await writeFile(join(scripts, 'portfolio.ts'), cli);
    }
  }
  await mkdir(join(target, 'scripts'), { recursive: true });
  await writeFile(join(target, 'scripts/portfolio.ts'), `import { readFile } from "node:fs/promises";\nimport { parse } from "${join(target, 'node_modules/yaml/dist/index.js')}";\nconst text = await readFile(process.env.AIDLC_RUNNER_PROFILE ?? ${JSON.stringify(profile)}, "utf8");\nconst frontmatter = text.match(/^---\\r?\\n([\\s\\S]*?)\\r?\\n---/);\nif (!frontmatter) throw new Error("Runner profile has no YAML frontmatter");\nconst configured = parse(frontmatter[1]).provider;\nconst provider = configured === "claude_code" ? "claude" : configured;\nif (!["claude", "codex"].includes(provider)) throw new Error("Unsupported runner provider: " + configured);\nconst index = Bun.argv.indexOf("--provider");\nif (index >= 0 && Bun.argv[index + 1] !== provider) throw new Error("--provider conflicts with runner profile");\nawait import("../runtimes/" + provider + "/scripts/portfolio.ts");\n`);
  let skill = await readFile(join(original, 'SKILL.md'), 'utf8');
  skill = skill.replace(/^name: aidlc-portfolio$/m, 'name: aidlc-portfolio-override');
  skill += '\n\n## Provider override\nUse this skill directory as SKILL_DIR for every portfolio command. The entrypoint reads the installed aidlc_runner profile on every invocation. For Codex, stage a Codex distribution containing .codex/config.toml, .agents/skills/aidlc/SKILL.md and aidlc/. Do not use the Claude source or --opus-model for Codex. The runner profile owns model selection. Existing tracked runtime paths must be reconciled by the operator; never bypass refusal.\n';
  await writeFile(join(target, 'SKILL.md'), skill);
}
if (import.meta.main) {
  if (!Bun.argv[2]) throw new Error('Usage: bun build.ts DESTINATION [ORIGINAL_SKILL]');
  await build(Bun.argv[2], Bun.argv[3]);
}
