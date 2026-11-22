import { test, expect } from 'bun:test';
import { mkdtemp, mkdir, writeFile, readFile, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { build } from '../overrides/build.ts';
test('Codex projection detects drift and protects tracked paths', async () => {
 const root = await mkdtemp('/tmp/portfolio-override-test-');
 try {
  await build(join(root, 'override'));
  const h = await import(join(root, 'override/runtimes/codex/scripts/harness.ts'));
  const source = join(root, 'source');
  for (const name of ['.codex', '.agents/skills/aidlc', 'aidlc']) await mkdir(join(source, name), { recursive: true });
  await writeFile(join(source, '.codex/config.toml'), '# config\n');
  await writeFile(join(source, '.agents/skills/aidlc/SKILL.md'), '# skill\n');
  await writeFile(join(source, 'aidlc/version.txt'), '1');
  const wt = join(root, 'worktrees/api');
  await mkdir(wt, { recursive: true });
  async function git(...args: string[]) {
   const p = Bun.spawn(['git', '-C', wt, ...args], { stdout: 'pipe', stderr: 'pipe' });
   if (await p.exited) throw new Error(await new Response(p.stderr).text());
  }
  await git('init');
  await mkdir(join(root, 'portfolio/intents'), { recursive: true });
  await writeFile(join(root, 'portfolio/intents/f.yaml'), 'id: f\nprojects:\n  - project: api\n    worktree: worktrees/api\n');
  expect((await h.stageHarness(root, 'codex', source)).changed).toBe(true);
  expect((await h.stageHarness(root, 'codex', source)).changed).toBe(false);
  await h.syncHarness(root, 'codex');
  expect((await h.verifyHarness(root, 'codex')).ok).toBe(true);
  expect((await h.syncHarness(root, 'codex')).changed).toBe(false);
  await writeFile(join(wt, '.agents/skills/aidlc/SKILL.md'), 'tampered');
  expect((await h.verifyHarness(root, 'codex')).ok).toBe(false);
  await h.syncHarness(root, 'codex');
  await git('add', '-f', '.codex/config.toml');
  await expect(h.syncHarness(root, 'codex')).rejects.toThrow('tracked harness paths');
  await writeFile(join(source, 'aidlc/version.txt'), '2');
  expect((await h.verifyHarness(root, 'codex')).ok).toBe(false);
  expect(await readFile(join(root, 'override/runtimes/codex/scripts/lib.ts'), 'utf8')).not.toContain('verifyHarness(root, "claude"');
 } finally { await rm(root, { recursive: true, force: true }); }
});

test('wrapper selects runner provider for CLI and rejects conflicting flags', async () => {
 const root = await mkdtemp('/tmp/portfolio-profile-test-');
 try {
  const profile = join(root, 'runner.md');
  await writeFile(profile, '---\nprovider: codex\n---\n');
  const output = join(root, 'override');
  await build(output, undefined, profile);
  async function run(...args: string[]) {
   const p = Bun.spawn(['bun', join(output, 'scripts/portfolio.ts'), ...args], { stdout: 'pipe', stderr: 'pipe' });
   return { code: await p.exited, out: await new Response(p.stdout).text(), err: await new Response(p.stderr).text() };
  }
  const rejected = await run('harness', 'verify', '--root', root, '--provider', 'claude');
  expect(rejected.code).not.toBe(0);
  expect(rejected.err).toContain('conflicts with runner profile');
  const codex = await run('harness', 'verify', '--root', root);
  expect(codex.out).toContain('"provider": "codex"');
  await writeFile(profile, '---\nprovider: claude_code\n---\n');
  const claude = await run('harness', 'verify', '--root', root);
  expect(claude.out).toContain('"provider": "claude"');
  await writeFile(profile, '---\nprovider: unknown\n---\n');
  expect((await run('help')).code).not.toBe(0);
 } finally { await rm(root, { recursive: true, force: true }); }
});
