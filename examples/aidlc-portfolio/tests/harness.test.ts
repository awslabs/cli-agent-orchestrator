import { afterEach, describe, expect, test } from "bun:test";
import {
  mkdtemp,
  mkdir,
  readFile,
  rm,
  unlink,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { stringify } from "yaml";
import {
  stageHarness,
  syncHarness,
  verifyHarness,
} from "../skills/aidlc-portfolio/scripts/harness.ts";
import {
  initializeWorkspace,
  registerDocument,
} from "../skills/aidlc-portfolio/scripts/lib.ts";

const temporaryRoots: string[] = [];

afterEach(async () => {
  await Promise.all(
    temporaryRoots.splice(0).map((root) =>
      rm(root, { recursive: true, force: true }),
    ),
  );
});

describe("portfolio harness lifecycle", () => {
  test("stages, overlays, synchronizes, and verifies idempotently", async () => {
    const fixture = await createFixture();

    const staged = await stageHarness(
      fixture.root,
      "claude",
      fixture.source,
    );
    const stagedAgain = await stageHarness(
      fixture.root,
      "claude",
      fixture.source,
    );
    expect(staged.changed).toBe(true);
    expect(stagedAgain.changed).toBe(false);
    expect(stagedAgain.manifestRevision).toBe(staged.manifestRevision);
    expect(await opusModel(fixture.source)).toBe(
      "global.anthropic.claude-opus-4-8[1m]",
    );
    expect(await opusModel(join(fixture.root, "harness/claude"))).toBe(
      "global.anthropic.claude-opus-5[1m]",
    );

    const synced = await syncHarness(fixture.root, "claude");
    const syncedAgain = await syncHarness(fixture.root, "claude");
    expect(synced.changed).toBe(true);
    expect(syncedAgain.changed).toBe(false);
    expect(await opusModel(fixture.worktree)).toBe(
      "global.anthropic.claude-opus-5[1m]",
    );
    expect(await gitOutput(fixture.worktree, ["status", "--short"])).toBe("");

    const verification = await verifyHarness(fixture.root, "claude");
    expect(verification.ok).toBe(true);
    expect(verification.worktrees).toEqual([
      expect.objectContaining({
        project: "api",
        intent: "feature",
        path: fixture.worktree,
        ok: true,
      }),
    ]);
  });

  test("detects source upgrades and staged or worktree drift", async () => {
    const fixture = await createFixture();
    await stageHarness(fixture.root, "claude", fixture.source);
    await syncHarness(fixture.root, "claude");

    await writeFile(join(fixture.source, "aidlc/version.txt"), "source-v2\n");
    let verification = await verifyHarness(fixture.root, "claude");
    expect(verification.ok).toBe(false);
    expect(verification.errors.join("\n")).toContain("source harness changed");

    await stageHarness(fixture.root, "claude", fixture.source);
    verification = await verifyHarness(fixture.root, "claude");
    expect(verification.ok).toBe(false);
    expect(verification.worktrees[0]?.errors.join("\n")).toContain(
      "projected harness files differ",
    );
    await syncHarness(fixture.root, "claude");
    expect((await verifyHarness(fixture.root, "claude")).ok).toBe(true);

    await writeFile(
      join(fixture.root, "harness/claude/aidlc/version.txt"),
      "staged-drift\n",
    );
    verification = await verifyHarness(fixture.root, "claude");
    expect(verification.ok).toBe(false);
    expect(verification.errors.join("\n")).toContain(
      "staged harness files do not match",
    );

    await unlink(
      join(fixture.root, "harness/claude/.claude/skills/aidlc/SKILL.md"),
    );
    await stageHarness(fixture.root, "claude", fixture.source);
    expect(
      await readFile(
        join(fixture.root, "harness/claude/.claude/skills/aidlc/SKILL.md"),
        "utf8",
      ),
    ).toBe("# AI-DLC\n");
    await syncHarness(fixture.root, "claude");
    await writeFile(join(fixture.worktree, "aidlc/version.txt"), "worktree-drift\n");
    verification = await verifyHarness(fixture.root, "claude");
    expect(verification.ok).toBe(false);
    expect(verification.worktrees[0]?.errors.join("\n")).toContain(
      "projected harness files differ",
    );
  });

  test("detects and repairs Claude model overlay drift", async () => {
    const fixture = await createFixture();
    await stageHarness(fixture.root, "claude", fixture.source);
    await syncHarness(fixture.root, "claude");

    await setOpusModel(
      join(fixture.root, "harness/claude"),
      "global.anthropic.claude-opus-4-8[1m]",
    );
    let verification = await verifyHarness(fixture.root, "claude");
    expect(verification.ok).toBe(false);
    expect(verification.errors.join("\n")).toContain(
      "staged harness files do not match",
    );

    await stageHarness(fixture.root, "claude", fixture.source);
    await syncHarness(fixture.root, "claude");
    await setOpusModel(
      fixture.worktree,
      "global.anthropic.claude-opus-4-8[1m]",
    );
    verification = await verifyHarness(fixture.root, "claude");
    expect(verification.ok).toBe(false);
    expect(verification.worktrees[0]?.errors.join("\n")).toContain(
      "Claude model overlay differs",
    );
  });

  test("refuses to overwrite project-owned tracked harness files", async () => {
    const fixture = await createFixture();
    await mkdir(join(fixture.worktree, ".claude"), { recursive: true });
    await writeFile(join(fixture.worktree, ".claude/project-setting.txt"), "owned\n");
    await gitOutput(fixture.worktree, ["add", ".claude/project-setting.txt"]);
    await gitOutput(fixture.worktree, ["commit", "-m", "Add project settings"]);
    await stageHarness(fixture.root, "claude", fixture.source);

    await expect(syncHarness(fixture.root, "claude")).rejects.toThrow(
      "refusing to overwrite tracked harness paths",
    );
    expect(
      await readFile(
        join(fixture.worktree, ".claude/project-setting.txt"),
        "utf8",
      ),
    ).toBe("owned\n");
  });
});

async function createFixture(): Promise<{
  root: string;
  source: string;
  worktree: string;
}> {
  const root = await mkdtemp(join(tmpdir(), "aidlc-portfolio-harness-"));
  temporaryRoots.push(root);
  await initializeWorkspace(root, "sample-portfolio", "Sample Portfolio");

  const source = join(root, "source-distribution");
  await mkdir(join(source, ".claude/skills/aidlc"), { recursive: true });
  await mkdir(join(source, "aidlc"), { recursive: true });
  await writeFile(
    join(source, ".claude/settings.json"),
    `${JSON.stringify(
      {
        env: {
          ANTHROPIC_DEFAULT_OPUS_MODEL:
            "global.anthropic.claude-opus-4-8[1m]",
        },
        model: "opus[1m]",
      },
      null,
      2,
    )}\n`,
  );
  await writeFile(
    join(source, ".claude/skills/aidlc/SKILL.md"),
    "# AI-DLC\n",
  );
  await writeFile(join(source, "aidlc/version.txt"), "source-v1\n");

  await registerDocument(
    root,
    "project",
    await writeYaml(root, "api.yaml", {
      schemaVersion: 1,
      id: "api",
      name: "API",
      repository: "repositories/api",
      description: "API project",
      status: "proposed",
      businessCapabilities: [],
      components: [
        {
          id: "api-service",
          name: "API service",
          type: "service",
          criticality: "unknown",
        },
      ],
      owners: { team: "Unknown" },
      environments: [],
      evidence: [],
      confidence: "low",
      lastVerified: "2026-07-27",
    }),
  );
  await registerDocument(
    root,
    "intent",
    await writeYaml(root, "feature.yaml", {
      schemaVersion: 1,
      id: "feature",
      name: "Feature",
      objective: "Deliver the feature.",
      status: "proposed",
      businessOutcomes: [],
      projects: [
        {
          project: "api",
          aidlcIntent: "feature-api",
          aidlcSpace: "default",
          branch: "feat/feature-api",
          worktree: "worktrees/api/feature",
          dependsOn: [],
        },
      ],
      createdAt: "2026-07-27T00:00:00.000Z",
      updatedAt: "2026-07-27T00:00:00.000Z",
    }),
  );

  const worktree = join(root, "worktrees/api/feature");
  await mkdir(worktree, { recursive: true });
  await gitOutput(worktree, ["init", "-b", "main"]);
  await gitOutput(worktree, ["config", "user.name", "Portfolio Test"]);
  await gitOutput(worktree, ["config", "user.email", "portfolio@example.com"]);
  await writeFile(join(worktree, "README.md"), "# API\n");
  await gitOutput(worktree, ["add", "README.md"]);
  await gitOutput(worktree, ["commit", "-m", "Initial"]);

  return { root, source, worktree };
}

async function opusModel(root: string): Promise<string | undefined> {
  const settings = JSON.parse(
    await readFile(join(root, ".claude/settings.json"), "utf8"),
  ) as { env?: Record<string, string> };
  return settings.env?.ANTHROPIC_DEFAULT_OPUS_MODEL;
}

async function setOpusModel(root: string, model: string): Promise<void> {
  const path = join(root, ".claude/settings.json");
  const settings = JSON.parse(await readFile(path, "utf8")) as {
    env?: Record<string, string>;
  };
  settings.env ??= {};
  settings.env.ANTHROPIC_DEFAULT_OPUS_MODEL = model;
  await writeFile(path, `${JSON.stringify(settings, null, 2)}\n`);
}

async function writeYaml(
  root: string,
  name: string,
  value: unknown,
): Promise<string> {
  const file = join(root, name);
  await writeFile(file, stringify(value));
  return file;
}

async function gitOutput(directory: string, args: string[]): Promise<string> {
  const process = Bun.spawn(["git", "-C", directory, ...args], {
    stdout: "pipe",
    stderr: "pipe",
  });
  const [exitCode, stdout, stderr] = await Promise.all([
    process.exited,
    new Response(process.stdout).text(),
    new Response(process.stderr).text(),
  ]);
  if (exitCode !== 0) {
    throw new Error(
      `git ${args.join(" ")} failed: ${stderr.trim() || stdout.trim()}`,
    );
  }
  return stdout;
}
