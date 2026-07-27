import { afterEach, describe, expect, test } from "bun:test";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { stringify } from "yaml";
import {
  advanceLifecycle,
  completeLifecycle,
  checkDispatch,
  confirmDiscovery,
  initializeWorkspace,
  lifecycleStatus,
  migrateLifecycle,
  registerDocument,
  registerLearningProposal,
  rejectLearningProposal,
  updateSession,
} from "../skills/aidlc-portfolio/scripts/lib.ts";
import {
  stageHarness,
  syncHarness,
} from "../skills/aidlc-portfolio/scripts/harness.ts";
import { submitChildResult } from "../skills/aidlc-portfolio/scripts/convergence.ts";

const temporaryRoots: string[] = [];

afterEach(async () => {
  await Promise.all(
    temporaryRoots.splice(0).map((root) =>
      rm(root, { recursive: true, force: true }),
    ),
  );
});

describe("portfolio lifecycle", () => {
  test("persists guarded transitions and completes idempotently", async () => {
    const root = await createFixture();
    let status = await lifecycleStatus(root);
    expect(status.phase).toBe("bootstrap");
    expect(status.actions).toEqual(["lifecycle advance --to discover"]);

    await expect(advanceLifecycle(root, "confirm")).rejects.toThrow(
      "next phase is discover",
    );
    expect((await advanceLifecycle(root, "discover")).changed).toBe(true);
    expect((await advanceLifecycle(root, "discover")).changed).toBe(false);
    await confirmDefaultDiscovery(root);
    await advanceLifecycle(root, "confirm");
    await advanceLifecycle(root, "plan");

    status = await lifecycleStatus(root);
    expect(status.phase).toBe("plan");
    expect(status.actions).toEqual([
      "lifecycle advance --to dispatch --accepted-by <human>",
    ]);
    expect(status.blockers).toContain(
      "record explicit plan acceptance with --accepted-by <human>",
    );
    await expect(advanceLifecycle(root, "dispatch")).rejects.toThrow(
      "record explicit plan acceptance",
    );

    await advanceLifecycle(root, "dispatch", "Portfolio Owner");
    await updateSession(root, "feature", "api", "active", "runner-1");
    await expect(advanceLifecycle(root, "integrate")).rejects.toThrow(
      "child session feature:api is active",
    );
    await updateSession(root, "feature", "api", "completed");
    await submitLifecycleResult(root);
    await advanceLifecycle(root, "integrate");
    await advanceLifecycle(root, "learn");
    expect((await completeLifecycle(root, "Portfolio Owner")).changed).toBe(true);
    expect((await completeLifecycle(root, "Portfolio Owner")).changed).toBe(false);

    status = await lifecycleStatus(root);
    expect(status).toMatchObject({
      status: "completed",
      phase: "learn",
      actions: [],
    });
    expect(status.history.map((event) => event.to)).toEqual([
      "bootstrap",
      "discover",
      "confirm",
      "plan",
      "dispatch",
      "integrate",
      "learn",
      "learn",
    ]);
  });

  test("rejects stale discovery before dispatch and reports recovery action", async () => {
    const root = await createFixture();
    await advanceLifecycle(root, "discover");
    await confirmDefaultDiscovery(root);
    await advanceLifecycle(root, "confirm");
    await advanceLifecycle(root, "plan");
    await registerProject(root, "worker");

    await expect(
      advanceLifecycle(root, "dispatch", "Portfolio Owner"),
    ).rejects.toThrow("discovery confirmation is stale");
    const status = await lifecycleStatus(root);
    expect(status.phase).toBe("plan");
    expect(status.blockers.join("\n")).toContain("discovery confirmation is stale");

    await confirmDefaultDiscovery(root);
    await expect(
      advanceLifecycle(root, "dispatch", "Portfolio Owner"),
    ).resolves.toMatchObject({ phase: "dispatch" });

    await registerProject(root, "reporting");
    await confirmDefaultDiscovery(root);
    await expect(checkDispatch(root, "api", "feature")).rejects.toThrow(
      "plan acceptance is stale",
    );
    expect(
      await advanceLifecycle(root, "dispatch", "Portfolio Owner"),
    ).toMatchObject({ changed: true, phase: "dispatch" });
    await expect(checkDispatch(root, "api", "feature")).resolves.toMatchObject({
      aidlcIntent: "feature-api",
    });
  });

  test("blocks integration completion on contracts and shared-memory proposals", async () => {
    const root = await createFixture();
    await registerContractDependency(root, "proposed");
    await advanceLifecycle(root, "discover");
    await confirmDefaultDiscovery(root, ["api-contract"]);
    await advanceLifecycle(root, "confirm");
    await advanceLifecycle(root, "plan");
    await advanceLifecycle(root, "dispatch", "Portfolio Owner");
    await updateSession(root, "feature", "api", "completed");
    await submitLifecycleResult(root, ["api-contract"]);
    await advanceLifecycle(root, "integrate");

    await expect(advanceLifecycle(root, "learn")).rejects.toThrow(
      "contract dependency api-contract is proposed",
    );

    await registerContractDependency(root, "verified");
    await confirmDefaultDiscovery(root, ["api-contract"]);
    const proposal = await writeYaml(root, "pending-learning.yaml", {
      schemaVersion: 1,
      id: "pending-learning",
      project: "api",
      intent: "feature",
      space: "default",
      destination: "project",
      heading: "Corrections",
      rule: "Keep integration decisions explicit.",
      evidence: ["Observed during integration."],
      source: { stage: "build-and-test" },
      baseRevision:
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    });
    await registerLearningProposal(root, proposal);
    await expect(advanceLifecycle(root, "learn")).rejects.toThrow(
      "shared-memory proposal pending-learning is pending",
    );

    await rejectLearningProposal(root, "pending-learning", "Not durable.");
    await expect(advanceLifecycle(root, "learn")).resolves.toMatchObject({
      phase: "learn",
    });
  });

  test("migrates schemaVersion 1 state once and resumes deterministically", async () => {
    const root = await createFixture();
    const statePath = join(root, "portfolio/state.json");
    const current = JSON.parse(await readFile(statePath, "utf8")) as Record<
      string,
      unknown
    >;
    delete current.lifecycle;
    current.schemaVersion = 1;
    await writeFile(statePath, `${JSON.stringify(current, null, 2)}\n`);

    await expect(lifecycleStatus(root)).rejects.toThrow(
      "run lifecycle migrate",
    );
    expect(await migrateLifecycle(root)).toEqual({
      migrated: true,
      phase: "discover",
    });
    expect(await migrateLifecycle(root)).toEqual({
      migrated: false,
      phase: "discover",
    });
    expect(await lifecycleStatus(root)).toMatchObject({
      status: "running",
      phase: "discover",
      actions: ["lifecycle advance --to confirm"],
    });
  });
});

async function createFixture(): Promise<string> {
  const root = await mkdtemp(join(tmpdir(), "aidlc-portfolio-lifecycle-"));
  temporaryRoots.push(root);
  await initializeWorkspace(root, "sample-portfolio", "Sample Portfolio");
  await registerProject(root, "api");
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

  const source = join(root, "source-distribution");
  await mkdir(join(source, ".claude/skills/aidlc"), { recursive: true });
  await mkdir(join(source, "aidlc"), { recursive: true });
  await writeFile(
    join(source, ".claude/settings.json"),
    `${JSON.stringify(
      {
        env: {
          ANTHROPIC_DEFAULT_OPUS_MODEL:
            "global.anthropic.claude-opus-5[1m]",
        },
        model: "opus[1m]",
      },
      null,
      2,
    )}\n`,
  );
  await writeFile(join(source, ".claude/skills/aidlc/SKILL.md"), "# AI-DLC\n");
  await writeFile(join(source, "aidlc/version.txt"), "test\n");
  await stageHarness(root, "claude", source);
  await syncHarness(root, "claude");
  return root;
}

async function registerProject(root: string, id: string): Promise<void> {
  await registerDocument(
    root,
    "project",
    await writeYaml(root, `${id}.yaml`, {
      schemaVersion: 1,
      id,
      name: id.toUpperCase(),
      repository: `repositories/${id}`,
      description: `${id} project`,
      status: "proposed",
      businessCapabilities: [],
      components: [
        {
          id: `${id}-service`,
          name: `${id} service`,
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
}

async function registerContractDependency(
  root: string,
  status: "proposed" | "verified",
): Promise<void> {
  await registerDocument(
    root,
    "dependency",
    await writeYaml(root, `api-contract-${status}.yaml`, {
      schemaVersion: 1,
      id: "api-contract",
      source: { project: "api", component: "api-service" },
      target: { project: "api", component: "api-service" },
      type: "contract",
      blockingAt: ["integration"],
      status,
      confidence: status === "verified" ? "high" : "low",
      evidence: [],
      lastVerified: "2026-07-27",
    }),
  );
}

async function confirmDefaultDiscovery(
  root: string,
  dependencies: string[] = [],
): Promise<void> {
  await confirmDiscovery(
    root,
    await writeYaml(root, `discovery-${crypto.randomUUID()}.yaml`, {
      schemaVersion: 1,
      organization: { disposition: "unknown", value: "Unknown" },
      businessOutcomes: { disposition: "unknown", values: [] },
      businessCapabilities: { disposition: "unknown", values: [] },
      dependencies: { disposition: "confirmed", ids: dependencies },
      acceptance: {
        acceptedBy: "Portfolio Owner",
        acceptedAt: "2026-07-27T00:00:00.000Z",
        unknowns: [
          "organization",
          "businessOutcomes",
          "businessCapabilities",
        ],
        deferrals: [],
      },
    }),
  );
}

async function submitLifecycleResult(
  root: string,
  dependencies: string[] = [],
): Promise<void> {
  const file = await writeYaml(root, `result-${crypto.randomUUID()}.yaml`, {
    schemaVersion: 1,
    project: "api",
    intent: "feature",
    changedComponents: [],
    changedCapabilities: [],
    contracts: dependencies.map((dependency) => ({
      dependency,
      change: "compatible",
      evidence: ["Lifecycle fixture compatibility check passed."],
    })),
    dependencyAssumptions: dependencies.map((dependency) => ({
      dependency,
      disposition: "satisfied",
      note: "Lifecycle fixture dependency is satisfied.",
    })),
    verification: ["Lifecycle fixture verification passed."],
    submittedAt: "2026-07-27T00:00:00.000Z",
  });
  await submitChildResult(root, file);
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
