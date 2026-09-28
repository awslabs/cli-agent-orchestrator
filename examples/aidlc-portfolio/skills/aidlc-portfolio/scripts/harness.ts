import { createHash } from "node:crypto";
import {
  cp,
  lstat,
  mkdir,
  readFile,
  readdir,
  rename,
  rm,
  writeFile,
} from "node:fs/promises";
import { dirname, join, relative, resolve, sep } from "node:path";
import { parse } from "yaml";

export type HarnessProvider = "claude";

interface HarnessManifest {
  schemaVersion: 1;
  provider: HarnessProvider;
  source: {
    path: string;
    revision: string;
    files: Record<string, string>;
  };
  overlay: {
    opusModel: string;
    revision: string;
  };
  staged: {
    files: Record<string, string>;
  };
  stagedAt: string;
}

interface HarnessReceipt {
  schemaVersion: 1;
  provider: HarnessProvider;
  manifestRevision: string;
  files: Record<string, string>;
  syncedAt: string;
}

interface RegisteredWorktree {
  project: string;
  intent: string;
  path: string;
}

export interface HarnessVerification {
  ok: boolean;
  provider: HarnessProvider;
  manifestPath: string;
  manifestRevision: string | null;
  errors: string[];
  worktrees: Array<{
    project: string;
    intent: string;
    path: string;
    ok: boolean;
    errors: string[];
  }>;
}

const DEFAULT_OPUS_MODEL = "global.anthropic.claude-opus-5[1m]";
const MANIFEST_FILE = "manifest.json";
const RECEIPT_FILE = join(".aidlc-portfolio", "harness-manifest.json");
const EXCLUDE_BLOCK = [
  "# aidlc-portfolio runtime begin",
  "/.claude/",
  "/aidlc/",
  "/.aidlc-portfolio/",
  "# aidlc-portfolio runtime end",
].join("\n");

export class HarnessError extends Error {}

export async function stageHarness(
  rootInput: string,
  providerInput: string,
  sourceInput: string,
  opusModel = DEFAULT_OPUS_MODEL,
): Promise<{
  changed: boolean;
  path: string;
  manifestPath: string;
  manifestRevision: string;
}> {
  const provider = requireProvider(providerInput);
  const root = resolve(rootInput);
  const source = resolve(sourceInput);
  const target = harnessRoot(root, provider);
  if (pathsOverlap(source, target)) {
    throw new HarnessError(
      "harness source and staged target must not contain one another",
    );
  }
  await validateDistribution(source);

  const sourceFiles = await distributionFiles(source);
  const sourceRevision = await sourceRevisionFor(source, sourceFiles);
  const overlayRevision = revisionOf({ provider, opusModel });
  const existing = await readManifest(target);
  const existingStagedFiles = existing
    ? await optionalDistributionFiles(target)
    : null;
  const existingOpusModel = existing ? await optionalConfiguredOpusModel(target) : null;
  if (
    existing &&
    existing.source.path === source &&
    existing.source.revision === sourceRevision &&
    mapsEqual(existing.source.files, sourceFiles) &&
    existing.overlay.opusModel === opusModel &&
    existing.overlay.revision === overlayRevision &&
    existingStagedFiles &&
    mapsEqual(existing.staged.files, existingStagedFiles) &&
    existingOpusModel === opusModel
  ) {
    return {
      changed: false,
      path: target,
      manifestPath: join(target, MANIFEST_FILE),
      manifestRevision: revisionOf(existing),
    };
  }

  const parent = dirname(target);
  const temporary = join(parent, `.claude-stage-${crypto.randomUUID()}`);
  await mkdir(parent, { recursive: true });
  try {
    await cp(join(source, ".claude"), join(temporary, ".claude"), {
      recursive: true,
      errorOnExist: true,
    });
    await cp(join(source, "aidlc"), join(temporary, "aidlc"), {
      recursive: true,
      errorOnExist: true,
    });
    await applyClaudeOverlay(temporary, opusModel);
    const manifest: HarnessManifest = {
      schemaVersion: 1,
      provider,
      source: {
        path: source,
        revision: sourceRevision,
        files: sourceFiles,
      },
      overlay: {
        opusModel,
        revision: overlayRevision,
      },
      staged: {
        files: await distributionFiles(temporary),
      },
      stagedAt: new Date().toISOString(),
    };
    await writeJson(join(temporary, MANIFEST_FILE), manifest);
    await replaceDirectory(temporary, target);
    return {
      changed: true,
      path: target,
      manifestPath: join(target, MANIFEST_FILE),
      manifestRevision: revisionOf(manifest),
    };
  } finally {
    await rm(temporary, { recursive: true, force: true });
  }
}

export async function syncHarness(
  rootInput: string,
  providerInput: string,
  project?: string,
  intent?: string,
): Promise<{
  changed: boolean;
  manifestRevision: string;
  worktrees: Array<RegisteredWorktree & { changed: boolean }>;
}> {
  const provider = requireProvider(providerInput);
  const root = resolve(rootInput);
  const staged = harnessRoot(root, provider);
  const manifest = await requireCleanStagedHarness(staged);
  const manifestRevision = revisionOf(manifest);
  const worktrees = await registeredWorktrees(root, project, intent);
  if (worktrees.length === 0) {
    throw new HarnessError("no registered worktrees match the requested filters");
  }

  const results = [];
  for (const worktree of worktrees) {
    results.push({
      ...worktree,
      changed: await syncWorktree(worktree.path, staged, manifest, manifestRevision),
    });
  }
  return {
    changed: results.some((item) => item.changed),
    manifestRevision,
    worktrees: results,
  };
}

export async function verifyHarness(
  rootInput: string,
  providerInput: string,
  project?: string,
  intent?: string,
): Promise<HarnessVerification> {
  const provider = requireProvider(providerInput);
  const root = resolve(rootInput);
  const staged = harnessRoot(root, provider);
  const manifestPath = join(staged, MANIFEST_FILE);
  const errors: string[] = [];
  let manifest: HarnessManifest | null = null;

  try {
    manifest = await requireCleanStagedHarness(staged);
    const sourceFiles = await distributionFiles(manifest.source.path);
    const sourceRevision = await sourceRevisionFor(manifest.source.path, sourceFiles);
    if (
      sourceRevision !== manifest.source.revision ||
      !mapsEqual(sourceFiles, manifest.source.files)
    ) {
      errors.push(`source harness changed: ${manifest.source.path}`);
    }
  } catch (error) {
    errors.push(errorMessage(error));
  }

  const manifestRevision = manifest ? revisionOf(manifest) : null;
  const worktreeResults: HarnessVerification["worktrees"] = [];
  if (manifest) {
    const worktrees = await registeredWorktrees(root, project, intent);
    if (worktrees.length === 0) {
      errors.push("no registered worktrees match the requested filters");
    }
    for (const worktree of worktrees) {
      const worktreeErrors = await verifyWorktree(
        worktree.path,
        manifest,
        manifestRevision!,
      );
      worktreeResults.push({
        ...worktree,
        ok: worktreeErrors.length === 0,
        errors: worktreeErrors,
      });
    }
  }

  return {
    ok: errors.length === 0 && worktreeResults.every((item) => item.ok),
    provider,
    manifestPath,
    manifestRevision,
    errors,
    worktrees: worktreeResults,
  };
}

async function syncWorktree(
  worktree: string,
  staged: string,
  manifest: HarnessManifest,
  manifestRevision: string,
): Promise<boolean> {
  await requireGitWorktree(worktree);
  const tracked = await gitOutput(worktree, [
    "ls-files",
    "--",
    ".claude",
    "aidlc",
    ".aidlc-portfolio",
  ]);
  if (tracked.trim()) {
    throw new HarnessError(
      `refusing to overwrite tracked harness paths in ${worktree}: ${tracked
        .trim()
        .split(/\r?\n/)
        .join(", ")}`,
    );
  }

  const verification = await verifyWorktree(worktree, manifest, manifestRevision);
  if (verification.length === 0 && (await hasExcludeBlock(worktree))) {
    return false;
  }

  const temporary = join(worktree, `.aidlc-portfolio-sync-${crypto.randomUUID()}`);
  try {
    await cp(join(staged, ".claude"), join(temporary, ".claude"), {
      recursive: true,
      errorOnExist: true,
    });
    await cp(join(staged, "aidlc"), join(temporary, "aidlc"), {
      recursive: true,
      errorOnExist: true,
    });
    await replaceWorktreeDistribution(temporary, worktree);
    const receipt: HarnessReceipt = {
      schemaVersion: 1,
      provider: manifest.provider,
      manifestRevision,
      files: manifest.staged.files,
      syncedAt: new Date().toISOString(),
    };
    await writeJson(join(worktree, RECEIPT_FILE), receipt);
    await ensureExcludeBlock(worktree);
    return true;
  } finally {
    await rm(temporary, { recursive: true, force: true });
  }
}

async function verifyWorktree(
  worktree: string,
  manifest: HarnessManifest,
  manifestRevision: string,
): Promise<string[]> {
  const errors: string[] = [];
  try {
    await requireGitWorktree(worktree);
    const files = await distributionFiles(worktree);
    if (!mapsEqual(files, manifest.staged.files)) {
      errors.push("projected harness files differ from the staged manifest");
    }
    if ((await configuredOpusModel(worktree)) !== manifest.overlay.opusModel) {
      errors.push(`Claude model overlay differs from ${manifest.overlay.opusModel}`);
    }
    const receipt = await readJson<HarnessReceipt>(join(worktree, RECEIPT_FILE));
    if (receipt.manifestRevision !== manifestRevision) {
      errors.push("worktree receipt references a different harness manifest");
    }
    if (!mapsEqual(receipt.files, manifest.staged.files)) {
      errors.push("worktree receipt file hashes differ from the staged manifest");
    }
    if (!(await hasExcludeBlock(worktree))) {
      errors.push("repository-local harness exclusions are missing");
    }
  } catch (error) {
    errors.push(errorMessage(error));
  }
  return errors;
}

async function requireCleanStagedHarness(staged: string): Promise<HarnessManifest> {
  const manifest = await readManifest(staged);
  if (!manifest) {
    throw new HarnessError(`staged harness manifest not found: ${staged}`);
  }
  const files = await distributionFiles(staged);
  if (!mapsEqual(files, manifest.staged.files)) {
    throw new HarnessError(`staged harness files do not match ${join(staged, MANIFEST_FILE)}`);
  }
  if ((await configuredOpusModel(staged)) !== manifest.overlay.opusModel) {
    throw new HarnessError(
      `staged Claude model does not match overlay ${manifest.overlay.opusModel}`,
    );
  }
  return manifest;
}

async function registeredWorktrees(
  root: string,
  projectFilter?: string,
  intentFilter?: string,
): Promise<RegisteredWorktree[]> {
  const directory = join(root, "portfolio", "intents");
  const names = (await readdir(directory))
    .filter((name) => name.endsWith(".yaml") || name.endsWith(".yml"))
    .sort();
  const worktrees: RegisteredWorktree[] = [];
  const owners = new Map<string, string>();
  for (const name of names) {
    const document = parse(await readFile(join(directory, name), "utf8")) as {
      id?: string;
      projects?: Array<{ project?: string; worktree?: string }>;
    };
    for (const mapping of document.projects ?? []) {
      if (!document.id || !mapping.project || !mapping.worktree) continue;
      if (projectFilter && mapping.project !== projectFilter) continue;
      if (intentFilter && document.id !== intentFilter) continue;
      const path = resolveInside(root, mapping.worktree, join(root, "worktrees"));
      const owner = `${document.id}:${mapping.project}`;
      const previousOwner = owners.get(path);
      if (previousOwner && previousOwner !== owner) {
        throw new HarnessError(
          `registered worktree ${path} is shared by ${previousOwner} and ${owner}`,
        );
      }
      owners.set(path, owner);
      worktrees.push({
        project: mapping.project,
        intent: document.id,
        path,
      });
    }
  }
  return worktrees;
}

async function validateDistribution(root: string): Promise<void> {
  for (const required of [
    join(".claude", "settings.json"),
    join(".claude", "skills", "aidlc", "SKILL.md"),
    "aidlc",
  ]) {
    try {
      await lstat(join(root, required));
    } catch {
      throw new HarnessError(`Claude distribution is missing ${required}: ${root}`);
    }
  }
}

async function distributionFiles(root: string): Promise<Record<string, string>> {
  await validateDistribution(root);
  const files: Record<string, string> = {};
  await collectFiles(root, ".claude", files);
  await collectFiles(root, "aidlc", files);
  return files;
}

async function collectFiles(
  root: string,
  relativePath: string,
  files: Record<string, string>,
): Promise<void> {
  const path = join(root, relativePath);
  const entries = await readdir(path, { withFileTypes: true });
  for (const entry of entries.sort((left, right) => left.name.localeCompare(right.name))) {
    const childRelative = join(relativePath, entry.name);
    const child = join(root, childRelative);
    if (entry.isSymbolicLink()) {
      throw new HarnessError(`symbolic links are not supported in harnesses: ${child}`);
    }
    if (entry.isDirectory()) {
      await collectFiles(root, childRelative, files);
    } else if (entry.isFile()) {
      files[toPortablePath(childRelative)] = createHash("sha256")
        .update(await readFile(child))
        .digest("hex");
    }
  }
}

async function applyClaudeOverlay(root: string, opusModel: string): Promise<void> {
  if (!opusModel.trim()) {
    throw new HarnessError("Opus model overlay must not be empty");
  }
  const settingsPath = join(root, ".claude", "settings.json");
  const settings = await readJson<Record<string, unknown>>(settingsPath);
  const env =
    settings.env && typeof settings.env === "object"
      ? (settings.env as Record<string, unknown>)
      : {};
  env.ANTHROPIC_DEFAULT_OPUS_MODEL = opusModel;
  settings.env = env;
  await writeFile(settingsPath, `${JSON.stringify(settings, null, 2)}\n`);
}

async function configuredOpusModel(root: string): Promise<string | null> {
  const settings = await readJson<{ env?: Record<string, string> }>(
    join(root, ".claude", "settings.json"),
  );
  return settings.env?.ANTHROPIC_DEFAULT_OPUS_MODEL ?? null;
}

async function optionalDistributionFiles(
  root: string,
): Promise<Record<string, string> | null> {
  try {
    return await distributionFiles(root);
  } catch {
    return null;
  }
}

async function optionalConfiguredOpusModel(root: string): Promise<string | null> {
  try {
    return await configuredOpusModel(root);
  } catch {
    return null;
  }
}

async function readManifest(root: string): Promise<HarnessManifest | null> {
  try {
    return await readJson<HarnessManifest>(join(root, MANIFEST_FILE));
  } catch {
    return null;
  }
}

async function sourceRevisionFor(
  source: string,
  files: Record<string, string>,
): Promise<string> {
  try {
    return (await gitOutput(source, ["rev-parse", "HEAD"])).trim();
  } catch {
    return `sha256:${revisionOf(files)}`;
  }
}

async function requireGitWorktree(worktree: string): Promise<void> {
  try {
    await gitOutput(worktree, ["rev-parse", "--is-inside-work-tree"]);
  } catch {
    throw new HarnessError(`registered worktree is not a Git checkout: ${worktree}`);
  }
}

async function ensureExcludeBlock(worktree: string): Promise<void> {
  if (await hasExcludeBlock(worktree)) return;
  const exclude = await excludePath(worktree);
  let content = "";
  try {
    content = await readFile(exclude, "utf8");
  } catch {
    // Git may not have created the repository-local exclude file yet.
  }
  const next = `${content.replace(/\s+$/, "")}${content.trim() ? "\n\n" : ""}${EXCLUDE_BLOCK}\n`;
  await mkdir(dirname(exclude), { recursive: true });
  await writeFile(exclude, next);
}

async function hasExcludeBlock(worktree: string): Promise<boolean> {
  try {
    return (await readFile(await excludePath(worktree), "utf8")).includes(EXCLUDE_BLOCK);
  } catch {
    return false;
  }
}

async function excludePath(worktree: string): Promise<string> {
  const path = (await gitOutput(worktree, ["rev-parse", "--git-path", "info/exclude"]))
    .trim();
  return resolve(worktree, path);
}

async function replaceDirectory(source: string, target: string): Promise<void> {
  const backup = `${target}.backup-${crypto.randomUUID()}`;
  let hadTarget = false;
  try {
    await lstat(target);
    hadTarget = true;
    await rename(target, backup);
  } catch (error) {
    if (hadTarget || !isMissingPathError(error)) throw error;
  }
  try {
    await mkdir(dirname(target), { recursive: true });
    await rename(source, target);
    if (hadTarget) await rm(backup, { recursive: true, force: true });
  } catch (error) {
    if (hadTarget) await rename(backup, target);
    throw error;
  }
}

async function replaceWorktreeDistribution(
  temporary: string,
  worktree: string,
): Promise<void> {
  const backup = join(worktree, `.aidlc-portfolio-backup-${crypto.randomUUID()}`);
  const names = [".claude", "aidlc"];
  const backedUp: string[] = [];
  const installed: string[] = [];
  await mkdir(backup);
  try {
    for (const name of names) {
      const target = join(worktree, name);
      try {
        await lstat(target);
        await rename(target, join(backup, name));
        backedUp.push(name);
      } catch (error) {
        if (!isMissingPathError(error)) throw error;
      }
    }
    for (const name of names) {
      await rename(join(temporary, name), join(worktree, name));
      installed.push(name);
    }
  } catch (error) {
    for (const name of installed.reverse()) {
      await rm(join(worktree, name), { recursive: true, force: true });
    }
    for (const name of backedUp) {
      await rename(join(backup, name), join(worktree, name));
    }
    throw error;
  } finally {
    await rm(backup, { recursive: true, force: true });
  }
}

async function gitOutput(worktree: string, args: string[]): Promise<string> {
  const process = Bun.spawn(["git", "-C", worktree, ...args], {
    stdout: "pipe",
    stderr: "pipe",
  });
  const [exitCode, stdout, stderr] = await Promise.all([
    process.exited,
    new Response(process.stdout).text(),
    new Response(process.stderr).text(),
  ]);
  if (exitCode !== 0) {
    throw new HarnessError(
      `git ${args.join(" ")} failed in ${worktree}: ${stderr.trim() || stdout.trim()}`,
    );
  }
  return stdout;
}

async function readJson<T>(path: string): Promise<T> {
  try {
    return JSON.parse(await readFile(path, "utf8")) as T;
  } catch (error) {
    throw new HarnessError(`cannot read JSON ${path}: ${errorMessage(error)}`);
  }
}

async function writeJson(path: string, value: unknown): Promise<void> {
  await mkdir(dirname(path), { recursive: true });
  const temporary = `${path}.${crypto.randomUUID()}.tmp`;
  try {
    await writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, {
      flag: "wx",
    });
    await rename(temporary, path);
  } finally {
    await rm(temporary, { force: true });
  }
}

function harnessRoot(root: string, provider: HarnessProvider): string {
  return join(root, "harness", provider);
}

function requireProvider(value: string): HarnessProvider {
  if (value !== "claude") {
    throw new HarnessError(`unsupported harness provider: ${value}`);
  }
  return value;
}

function resolveInside(root: string, value: string, boundary: string): string {
  const resolved = resolve(root, value);
  const relativePath = relative(resolve(boundary), resolved);
  if (
    relativePath === "" ||
    (relativePath !== ".." && !relativePath.startsWith(`..${sep}`))
  ) {
    return resolved;
  }
  throw new HarnessError(`${value} must resolve inside ${resolve(boundary)}`);
}

function pathsOverlap(left: string, right: string): boolean {
  return isInside(left, right) || isInside(right, left);
}

function isInside(parent: string, child: string): boolean {
  const relativePath = relative(parent, child);
  return (
    relativePath === "" ||
    (relativePath !== ".." && !relativePath.startsWith(`..${sep}`))
  );
}

function isMissingPathError(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "code" in error &&
    error.code === "ENOENT"
  );
}

function revisionOf(value: unknown): string {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}

function mapsEqual(
  left: Record<string, string>,
  right: Record<string, string>,
): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function toPortablePath(value: string): string {
  return value.split(sep).join("/");
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
