import { afterEach, describe, expect, test } from "bun:test";
import { createHash } from "node:crypto";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { stringify } from "yaml";
import {
  stageHarness,
  syncHarness,
} from "../skills/aidlc-portfolio/scripts/harness.ts";
import {
  advanceLifecycle,
  answerQuestionPacket,
  approveLearningProposal,
  cleanWorktreeMemory,
  confirmDiscovery,
  PortfolioError,
  checkDispatch,
  createWorktree,
  doctorWorkspace,
  initializeWorkspace,
  listLearningProposals,
  listQuestionPackets,
  memorySnapshot,
  pathExists,
  reconcileLearningProposal,
  refreshWorktreeMemory,
  registerDocument,
  registerLearningProposal,
  submitQuestionPacket,
  updateSession,
  validateWorkspace,
} from "../skills/aidlc-portfolio/scripts/lib.ts";

const temporaryRoots: string[] = [];

afterEach(async () => {
  await Promise.all(
    temporaryRoots.splice(0).map((root) => rm(root, { recursive: true, force: true })),
  );
});

describe("portfolio workspace", () => {
  test("initializes idempotently and passes doctor", async () => {
    const root = await temporaryRoot();
    const first = await initializeWorkspace(root, "sample-portfolio", "Sample Portfolio");
    const second = await initializeWorkspace(root, "sample-portfolio", "Sample Portfolio");
    const doctor = await doctorWorkspace(root);

    expect(first.created).toBe(true);
    expect(second.created).toBe(false);
    expect(doctor).toEqual({
      ok: true,
      errors: [],
      counts: { projects: 0, dependencies: 0, intents: 0 },
    });
    expect(
      await readFile(join(root, "portfolio/questions/portfolio-discovery.md"), "utf8"),
    ).toContain("[Answer]:");
  });

  test("rejects conflicting initialization", async () => {
    const root = await temporaryRoot();
    await initializeWorkspace(root, "sample-portfolio", "Sample Portfolio");

    expect(
      initializeWorkspace(root, "another-portfolio", "Another Portfolio"),
    ).rejects.toBeInstanceOf(PortfolioError);
  });

  test("rejects live locks and recovers locks owned by dead processes", async () => {
    const root = await temporaryRoot();
    await initializeWorkspace(root, "sample-portfolio", "Sample Portfolio");
    const lock = join(root, "portfolio/.portfolio.lock");
    await mkdir(lock);
    await writeFile(
      join(lock, "owner.json"),
      `${JSON.stringify({
        token: "live-owner",
        pid: process.pid,
        createdAt: new Date().toISOString(),
      })}\n`,
    );

    await expect(registerProject(root, "api")).rejects.toThrow(
      "portfolio is locked by another operation",
    );

    await rm(lock, { recursive: true });
    const exited = Bun.spawn(["true"]);
    await exited.exited;
    await mkdir(lock);
    await writeFile(
      join(lock, "owner.json"),
      `${JSON.stringify({
        token: "dead-owner",
        pid: exited.pid,
        createdAt: "2026-01-01T00:00:00.000Z",
      })}\n`,
    );

    await expect(registerProject(root, "api")).resolves.toBeUndefined();
    expect(await pathExists(lock)).toBe(false);
  });

  test("registers projects and initializes child session state", async () => {
    const root = await temporaryRoot();
    await initializeWorkspace(root, "sample-portfolio", "Sample Portfolio");
    await registerProject(root, "api");
    await registerIntent(root, {
      id: "feature",
      projects: [
        {
          project: "api",
          aidlcIntent: "feature-api",
          branch: "feat/feature-api",
          worktree: "worktrees/api/feature",
          dependsOn: [],
        },
      ],
    });

    const validation = await validateWorkspace(root);
    const state = JSON.parse(await readFile(join(root, "portfolio/state.json"), "utf8"));
    expect(validation.ok).toBe(true);
    expect(state.sessions["feature:api"].status).toBe("pending");
  });

  test("rejects a dependency with an unknown project", async () => {
    const root = await temporaryRoot();
    await initializeWorkspace(root, "sample-portfolio", "Sample Portfolio");
    await registerProject(root, "api");
    const dependencyFile = await writeYaml(root, "invalid-dependency.yaml", {
      schemaVersion: 1,
      id: "api-to-missing",
      source: { project: "api", component: "api-service" },
      target: { project: "missing" },
      type: "runtime",
      blockingAt: ["integration"],
      status: "proposed",
      confidence: "low",
      evidence: [],
      lastVerified: "2026-07-25",
    });

    expect(
      registerDocument(root, "dependency", dependencyFile),
    ).rejects.toThrow("target project is unknown");
  });

  test("restores an existing document when replacement validation fails", async () => {
    const root = await temporaryRoot();
    await initializeWorkspace(root, "sample-portfolio", "Sample Portfolio");
    await registerProject(root, "api");
    const projectPath = join(root, "portfolio/projects/api.yaml");
    const previous = await readFile(projectPath, "utf8");
    const replacement = await writeYaml(root, "replacement.yaml", {
      schemaVersion: 1,
      id: "api",
      name: "API",
      repository: "../outside",
      description: "Invalid replacement",
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
      lastVerified: "2026-07-25",
    });

    expect(registerDocument(root, "project", replacement)).rejects.toThrow(
      "must resolve inside",
    );
    expect(await readFile(projectPath, "utf8")).toBe(previous);
  });

  test("dispatch check enforces project dependencies and harness presence", async () => {
    const root = await temporaryRoot();
    await initializeWorkspace(root, "sample-portfolio", "Sample Portfolio");
    await registerProject(root, "api");
    await registerProject(root, "web");
    await registerIntent(root, {
      id: "feature",
      projects: [
        {
          project: "api",
          aidlcIntent: "feature-api",
          branch: "feat/feature-api",
          worktree: "worktrees/api/feature",
          dependsOn: [],
        },
        {
          project: "web",
          aidlcIntent: "feature-web",
          branch: "feat/feature-web",
          worktree: "worktrees/web/feature",
          dependsOn: ["api"],
        },
      ],
    });
    await createHarness(root, "api/feature");
    await createHarness(root, "web/feature");
    await confirmDefaultDiscovery(root);
    await advanceToDispatch(root);

    expect(checkDispatch(root, "web", "feature")).rejects.toThrow(
      "blocked by incomplete project api",
    );
    await expect(
      updateSession(root, "feature", "web", "active", "web-runner"),
    ).rejects.toThrow("blocked by incomplete project api");

    await updateSession(root, "feature", "api", "completed");
    await expect(checkDispatch(root, "web", "feature")).resolves.toMatchObject({
      branch: "feat/feature-web",
      aidlcIntent: "feature-web",
    });
    await expect(
      updateSession(root, "feature", "web", "active", "web-runner"),
    ).resolves.toBeUndefined();
  });

  test("dispatch check enforces catalog dependencies marked for dispatch", async () => {
    const root = await temporaryRoot();
    await initializeWorkspace(root, "sample-portfolio", "Sample Portfolio");
    await registerProject(root, "api");
    await registerProject(root, "web");
    await registerIntent(root, {
      id: "feature",
      projects: [
        {
          project: "api",
          aidlcIntent: "feature-api",
          branch: "feat/feature-api",
          worktree: "worktrees/api/feature",
          dependsOn: [],
        },
        {
          project: "web",
          aidlcIntent: "feature-web",
          branch: "feat/feature-web",
          worktree: "worktrees/web/feature",
          dependsOn: [],
        },
      ],
    });
    const dependencyFile = await writeYaml(root, "web-to-api.yaml", {
      schemaVersion: 1,
      id: "web-to-api",
      source: { project: "web", component: "web-service" },
      target: { project: "api", component: "api-service" },
      type: "contract",
      blockingAt: ["dispatch"],
      status: "verified",
      confidence: "high",
      evidence: [{ source: "contracts/api.yaml" }],
      lastVerified: "2026-07-25",
    });
    await registerDocument(root, "dependency", dependencyFile);
    await createHarness(root, "api/feature");
    await createHarness(root, "web/feature");
    await confirmDefaultDiscovery(root, ["web-to-api"]);
    await advanceToDispatch(root);

    expect(checkDispatch(root, "web", "feature")).rejects.toThrow(
      "blocked at dispatch by dependency web-to-api",
    );
    await expect(
      updateSession(root, "feature", "web", "active", "web-runner"),
    ).rejects.toThrow("blocked at dispatch by dependency web-to-api");

    await updateSession(root, "feature", "api", "completed");
    await expect(checkDispatch(root, "web", "feature")).resolves.toMatchObject({
      aidlcIntent: "feature-web",
    });
  });

  test("creates the registered Git worktree", async () => {
    const root = await temporaryRoot();
    await initializeWorkspace(root, "sample-portfolio", "Sample Portfolio");
    const repository = join(root, "repositories/api");
    await mkdir(repository, { recursive: true });
    await runGit(repository, ["init", "-b", "main"]);
    await runGit(repository, ["config", "user.name", "Portfolio Test"]);
    await runGit(repository, ["config", "user.email", "portfolio@example.com"]);
    await writeFile(join(repository, "README.md"), "# API\n");
    await runGit(repository, ["add", "README.md"]);
    await runGit(repository, ["commit", "-m", "Initial"]);
    await registerProject(root, "api");
    await registerIntent(root, {
      id: "feature",
      projects: [
        {
          project: "api",
          aidlcIntent: "feature-api",
          branch: "feat/feature-api",
          worktree: "worktrees/api/feature",
          dependsOn: [],
        },
      ],
    });

    const worktree = await createWorktree(
      root,
      "api",
      "feature",
      "feat/feature-api",
      "main",
    );

    expect(await pathExists(join(worktree, ".git"))).toBe(true);
    expect(await readFile(join(worktree, "README.md"), "utf8")).toBe("# API\n");

    const localMemory = join(
      worktree,
      "aidlc/spaces/default/memory/project.md",
    );
    await mkdir(join(localMemory, ".."), { recursive: true });
    await writeFile(localMemory, "## Corrections\n\n- Temporary child learning.\n");
    await expect(updateSession(root, "feature", "api", "completed")).rejects.toThrow(
      "contains worktree changes",
    );
    const snapshot = await memorySnapshot(root, "api", "feature", "project");
    expect(snapshot.mergeClean).toBe(false);
    await cleanWorktreeMemory(
      root,
      "api",
      "feature",
      "project",
      snapshot.worktreeRevision,
    );
    expect(await pathExists(localMemory)).toBe(false);
    await expect(
      updateSession(root, "feature", "api", "completed"),
    ).resolves.toBeUndefined();
  });

  test("blocks dispatch until portfolio discovery is explicitly accepted", async () => {
    const root = await temporaryRoot();
    await initializeWorkspace(root, "sample-portfolio", "Sample Portfolio");
    await registerProject(root, "api");
    await registerIntent(root, {
      id: "feature",
      projects: [
        {
          project: "api",
          aidlcIntent: "feature-api",
          branch: "feat/feature-api",
          worktree: "worktrees/api/feature",
          dependsOn: [],
        },
      ],
    });
    await createHarness(root, "api/feature");

    await expect(checkDispatch(root, "api", "feature")).rejects.toThrow(
      "portfolio discovery is not confirmed",
    );

    const invalid = await discoveryFile(root, {
      unknowns: [],
    });
    await expect(confirmDiscovery(root, invalid)).rejects.toThrow(
      "acceptance.unknowns must explicitly list",
    );

    await confirmDefaultDiscovery(root);
    await advanceToDispatch(root);
    await expect(checkDispatch(root, "api", "feature")).resolves.toMatchObject({
      aidlcIntent: "feature-api",
    });

    await writeFile(
      join(root, "worktrees/api/feature/.claude/settings.json"),
      "{}\n",
    );
    await expect(
      updateSession(root, "feature", "api", "active", "runner-terminal"),
    ).rejects.toThrow("AI-DLC harness verification failed");
    await syncHarness(root, "claude", "api", "feature");

    await registerProject(root, "worker");
    await expect(checkDispatch(root, "api", "feature")).rejects.toThrow(
      "discovery confirmation is stale",
    );
  });

  test("relays generated questions to a human without changing question text", async () => {
    const root = await temporaryRoot();
    await initializeWorkspace(root, "sample-portfolio", "Sample Portfolio");
    await registerProject(root, "api");
    await registerIntent(root, {
      id: "feature",
      projects: [
        {
          project: "api",
          aidlcIntent: "feature-api",
          branch: "feat/feature-api",
          worktree: "worktrees/api/feature",
          dependsOn: [],
        },
      ],
    });
    await confirmDefaultDiscovery(root);
    await createHarness(root, "api/feature");
    await advanceToDispatch(root);
    await updateSession(root, "feature", "api", "active", "runner-terminal");

    const generated = join(root, "generated-questions.md");
    const questionText =
      "# Design Questions\n\n## Storage\n\nA. SQL\nB. Object storage\n\n[Answer]:\n";
    await writeFile(generated, questionText);
    await submitQuestionPacket(
      root,
      "feature-storage",
      "api",
      "feature",
      "application-design",
      generated,
    );

    const stateWaiting = JSON.parse(
      await readFile(join(root, "portfolio/state.json"), "utf8"),
    );
    expect(stateWaiting.sessions["feature:api"].status).toBe("waiting");
    expect(await listQuestionPackets(root, "api", "feature", "pending")).toHaveLength(
      1,
    );
    await expect(
      updateSession(root, "feature", "api", "active"),
    ).rejects.toThrow("has unanswered human questions");

    const altered = join(root, "altered-answers.md");
    await writeFile(
      altered,
      questionText
        .replace("## Storage", "## Storage and cache")
        .replace("[Answer]:", "[Answer]: A. SQL"),
    );
    await expect(
      answerQuestionPacket(root, "feature-storage", altered, "chat", "Portfolio owner"),
    ).rejects.toThrow("changed generated question text");

    const answered = join(root, "human-answers.md");
    const answeredText = questionText.replace(
      "[Answer]:",
      "[Answer]: A. SQL, because transactional consistency is required.",
    );
    await writeFile(answered, answeredText);
    await answerQuestionPacket(
      root,
      "feature-storage",
      answered,
      "guided",
      "Portfolio owner",
    );

    expect(
      await readFile(
        join(root, "portfolio/questions/feature-storage.md"),
        "utf8",
      ),
    ).toBe(answeredText);
    const stateActive = JSON.parse(
      await readFile(join(root, "portfolio/state.json"), "utf8"),
    );
    expect(stateActive.sessions["feature:api"].status).toBe("active");
    await updateSession(root, "feature", "api", "completed");
    const stateCompleted = JSON.parse(
      await readFile(join(root, "portfolio/state.json"), "utf8"),
    );
    expect(stateCompleted.activeIntent).toBeNull();
  });

  test("preserves worktree memory when HEAD cannot be inspected", async () => {
    const root = await temporaryRoot();
    await initializeWorkspace(root, "sample-portfolio", "Sample Portfolio");
    await registerProject(root, "api");
    await registerIntent(root, {
      id: "feature",
      projects: [
        {
          project: "api",
          aidlcIntent: "feature-api",
          branch: "feat/feature-api",
          worktree: "worktrees/api/feature",
          dependsOn: [],
        },
      ],
    });
    const worktree = join(root, "worktrees/api/feature");
    await mkdir(worktree, { recursive: true });
    await runGit(worktree, ["init", "-b", "feat/feature-api"]);
    const memory = join(
      worktree,
      "aidlc/spaces/default/memory/project.md",
    );
    const content = "## Corrections\n\n- Keep this child learning.\n";
    await mkdir(join(memory, ".."), { recursive: true });
    await writeFile(memory, content);
    const revision = createHash("sha256").update(content).digest("hex");

    await expect(
      cleanWorktreeMemory(root, "api", "feature", "project", revision),
    ).rejects.toThrow("cannot inspect worktree memory");
    expect(await readFile(memory, "utf8")).toBe(content);
  });

  test("rejects question packets that a runner has already answered", async () => {
    const root = await temporaryRoot();
    await initializeWorkspace(root, "sample-portfolio", "Sample Portfolio");
    await registerProject(root, "api");
    await registerIntent(root, {
      id: "feature",
      projects: [
        {
          project: "api",
          aidlcIntent: "feature-api",
          branch: "feat/feature-api",
          worktree: "worktrees/api/feature",
          dependsOn: [],
        },
      ],
    });
    const file = join(root, "self-answered.md");
    await writeFile(file, "## Decision\n\n[Answer]: Assumed by runner\n");

    await expect(
      submitQuestionPacket(
        root,
        "self-answered",
        "api",
        "feature",
        "application-design",
        file,
      ),
    ).rejects.toThrow("must not contain answers");
  });

  test("serializes parallel learning proposals against canonical memory", async () => {
    const root = await temporaryRoot();
    await initializeWorkspace(root, "sample-portfolio", "Sample Portfolio");
    await registerProject(root, "api");
    await registerIntent(root, {
      id: "feature",
      projects: [
        {
          project: "api",
          aidlcIntent: "feature-api",
          branch: "feat/feature-api",
          worktree: "worktrees/api/feature",
          dependsOn: [],
        },
      ],
    });
    const canonical = join(
      root,
      "repositories/api/aidlc/spaces/default/memory/project.md",
    );
    const worktree = join(
      root,
      "worktrees/api/feature/aidlc/spaces/default/memory/project.md",
    );
    await mkdir(join(canonical, ".."), { recursive: true });
    await mkdir(join(worktree, ".."), { recursive: true });
    const initial = "# Project Memory\n\n## Corrections\n";
    await writeFile(canonical, initial);
    await writeFile(worktree, initial);
    const snapshot = await memorySnapshot(root, "api", "feature", "project");

    const first = await learningFile(root, "first-rule", snapshot.canonicalRevision, {
      rule: "Construct service clients lazily inside request handlers.",
    });
    const second = await learningFile(
      root,
      "second-rule",
      snapshot.canonicalRevision,
      {
        rule: "Require a focused test for each new request handler.",
      },
    );
    await registerLearningProposal(root, first);
    await registerLearningProposal(root, second);

    await expect(approveLearningProposal(root, "first-rule")).resolves.toMatchObject({
      outcome: "applied",
    });
    await expect(approveLearningProposal(root, "second-rule")).rejects.toThrow(
      "is stale",
    );

    await reconcileLearningProposal(
      root,
      "second-rule",
      "Reviewed against the newly applied client-lifecycle rule; no conflict.",
    );
    await expect(approveLearningProposal(root, "second-rule")).resolves.toMatchObject({
      outcome: "applied",
    });

    const content = await readFile(canonical, "utf8");
    expect(content).toContain("portfolio-learning:first-rule");
    expect(content).toContain("portfolio-learning:second-rule");
    const proposals = await listLearningProposals(root, "api");
    expect(proposals.map((proposal) => proposal.status)).toEqual([
      "applied",
      "applied",
    ]);

    await expect(updateSession(root, "feature", "api", "completed")).rejects.toThrow(
      "contains worktree changes",
    );
    const staleWorktree = await memorySnapshot(root, "api", "feature", "project");
    await expect(
      refreshWorktreeMemory(
        root,
        "api",
        "feature",
        "project",
        "0000000000000000000000000000000000000000000000000000000000000000",
      ),
    ).rejects.toThrow("changed after inspection");
    await refreshWorktreeMemory(
      root,
      "api",
      "feature",
      "project",
      staleWorktree.worktreeRevision,
    );
    await expect(
      updateSession(root, "feature", "api", "completed"),
    ).resolves.toBeUndefined();
  });

  test("treats an equivalent canonical rule as an idempotent approval", async () => {
    const root = await temporaryRoot();
    await initializeWorkspace(root, "sample-portfolio", "Sample Portfolio");
    await registerProject(root, "api");
    await registerIntent(root, {
      id: "feature",
      projects: [
        {
          project: "api",
          aidlcIntent: "feature-api",
          branch: "feat/feature-api",
          worktree: "worktrees/api/feature",
          dependsOn: [],
        },
      ],
    });
    const canonical = join(
      root,
      "repositories/api/aidlc/spaces/default/memory/project.md",
    );
    await mkdir(join(canonical, ".."), { recursive: true });
    await writeFile(
      canonical,
      "## Testing Posture\n\n- Require pytest coverage for every handler.\n",
    );
    const proposal = await learningFile(
      root,
      "duplicate-rule",
      "0000000000000000000000000000000000000000000000000000000000000000",
      {
        heading: "Testing Posture",
        rule: "Require pytest coverage for every handler.",
      },
    );
    await registerLearningProposal(root, proposal);

    const result = await approveLearningProposal(root, "duplicate-rule");
    const content = await readFile(canonical, "utf8");
    expect(result.outcome).toBe("already-present");
    expect(content.match(/Require pytest coverage/g)?.length).toBe(1);
  });

  test("rejects malformed and wrong-space learning proposals", async () => {
    const root = await temporaryRoot();
    await initializeWorkspace(root, "sample-portfolio", "Sample Portfolio");
    await registerProject(root, "api");
    await registerIntent(root, {
      id: "feature",
      projects: [
        {
          project: "api",
          aidlcIntent: "feature-api",
          aidlcSpace: "payments",
          branch: "feat/feature-api",
          worktree: "worktrees/api/feature",
          dependsOn: [],
        },
      ],
    });
    const malformed = await writeYaml(root, "malformed-learning.yaml", {
      schemaVersion: 1,
      id: "malformed-learning",
      project: "api",
    });
    await expect(registerLearningProposal(root, malformed)).rejects.toThrow(
      "learning proposal schema validation failed",
    );

    const wrongSpace = await learningFile(
      root,
      "wrong-space",
      "0000000000000000000000000000000000000000000000000000000000000000",
      { rule: "This proposal targets the wrong space." },
    );
    await expect(registerLearningProposal(root, wrongSpace)).rejects.toThrow(
      "uses AI-DLC space payments, not default",
    );
  });
});

async function temporaryRoot(): Promise<string> {
  const root = await mkdtemp(join(tmpdir(), "aidlc-portfolio-"));
  temporaryRoots.push(root);
  return root;
}

async function registerProject(root: string, id: string): Promise<void> {
  const file = await writeYaml(root, `${id}.yaml`, {
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
    lastVerified: "2026-07-25",
  });
  await registerDocument(root, "project", file);
}

async function registerIntent(
  root: string,
  input: { id: string; projects: unknown[] },
): Promise<void> {
  const file = await writeYaml(root, `${input.id}.yaml`, {
    schemaVersion: 1,
    id: input.id,
    name: input.id,
    objective: `${input.id} objective`,
    status: "proposed",
    businessOutcomes: [],
    projects: input.projects,
    createdAt: "2026-07-25T00:00:00.000Z",
    updatedAt: "2026-07-25T00:00:00.000Z",
  });
  await registerDocument(root, "intent", file);
}

async function createHarness(root: string, worktree: string): Promise<void> {
  const target = join(root, "worktrees", worktree);
  if (!(await pathExists(join(target, ".git")))) {
    await mkdir(target, { recursive: true });
    await runGit(target, ["init", "-b", "main"]);
    await runGit(target, ["config", "user.name", "Portfolio Test"]);
    await runGit(target, ["config", "user.email", "portfolio@example.com"]);
    await writeFile(join(target, "README.md"), "# Test worktree\n");
    await runGit(target, ["add", "README.md"]);
    await runGit(target, ["commit", "-m", "Initial"]);
  }

  const source = join(root, "test-harness-source");
  if (!(await pathExists(source))) {
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
    await writeFile(join(source, ".claude/skills/aidlc/SKILL.md"), "# AI-DLC\n");
    await writeFile(join(source, "aidlc/version.txt"), "test\n");
  }
  await stageHarness(root, "claude", source);
  const [project, intent] = worktree.split("/");
  await syncHarness(root, "claude", project, intent);
}

async function confirmDefaultDiscovery(
  root: string,
  dependencies: string[] = [],
): Promise<void> {
  await confirmDiscovery(root, await discoveryFile(root, { dependencies }));
}

async function advanceToDispatch(root: string): Promise<void> {
  await advanceLifecycle(root, "discover");
  await advanceLifecycle(root, "confirm");
  await advanceLifecycle(root, "plan");
  await advanceLifecycle(root, "dispatch", "Portfolio Test Owner");
}

async function discoveryFile(
  root: string,
  overrides: { dependencies?: string[]; unknowns?: string[] },
): Promise<string> {
  return writeYaml(root, `discovery-${crypto.randomUUID()}.yaml`, {
    schemaVersion: 1,
    organization: { disposition: "unknown", value: "Unknown" },
    businessOutcomes: { disposition: "unknown", values: [] },
    businessCapabilities: { disposition: "unknown", values: [] },
    dependencies: {
      disposition: "confirmed",
      ids: overrides.dependencies ?? [],
    },
    acceptance: {
      acceptedBy: "Portfolio Test Owner",
      acceptedAt: "2026-07-26T00:00:00.000Z",
      unknowns:
        overrides.unknowns ??
        ["organization", "businessOutcomes", "businessCapabilities"],
      deferrals: [],
    },
  });
}

async function learningFile(
  root: string,
  id: string,
  baseRevision: string,
  overrides: { heading?: string; rule: string },
): Promise<string> {
  return writeYaml(root, `${id}.yaml`, {
    schemaVersion: 1,
    id,
    project: "api",
    intent: "feature",
    space: "default",
    destination: "project",
    heading: overrides.heading ?? "Corrections",
    rule: overrides.rule,
    evidence: ["Observed while running the child AI-DLC intent."],
    source: { stage: "build-and-test" },
    baseRevision,
  });
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

async function runGit(directory: string, args: string[]): Promise<void> {
  const process = Bun.spawn(["git", "-C", directory, ...args], {
    stdout: "pipe",
    stderr: "pipe",
  });
  const [code, stdout, stderr] = await Promise.all([
    process.exited,
    new Response(process.stdout).text(),
    new Response(process.stderr).text(),
  ]);
  if (code !== 0) {
    throw new Error(
      `git ${args.join(" ")} failed: ${stderr.trim() || stdout.trim()}`,
    );
  }
}
