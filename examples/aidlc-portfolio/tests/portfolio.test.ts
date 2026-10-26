import { afterEach, describe, expect, test } from "bun:test";
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
  promoteLearningProposal,
  confirmDiscovery,
  PortfolioError,
  checkDispatch,
  createWorktree,
  doctorWorkspace,
  initializeWorkspace,
  listLearningProposals,
  listQuestionPackets,
  recordHumanApproval,
  registerDocument,
  registerLearningProposal,
  rejectLearningProposal,
  submitQuestionPacket,
  updateSession,
  validateWorkspace,
  pathExists,
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
  }, 20_000);

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
  }, 20_000);

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

    // Per-space worktree memory is now LOCAL scratch, not a merge barrier:
    // durable learnings live in CAO memory, so a worktree project.md no longer
    // blocks session completion.
    const localMemory = join(
      worktree,
      "aidlc/spaces/default/memory/project.md",
    );
    await mkdir(join(localMemory, ".."), { recursive: true });
    await writeFile(localMemory, "## Corrections\n\n- Temporary child learning.\n");
    await expect(
      updateSession(root, "feature", "api", "completed"),
    ).resolves.toBeUndefined();
    expect(await readFile(localMemory, "utf8")).toBe(
      "## Corrections\n\n- Temporary child learning.\n",
    );
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
  }, 20_000);

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

    const answered = join(root, "human-answers.md");
    const answeredText = questionText.replace(
      "[Answer]:",
      "[Answer]: A. SQL, because transactional consistency is required.",
    );
    await writeFile(answered, answeredText);

    // MECHANISM: the answerer is not a free-text argument. An id with no
    // matching, unconsumed approval record is refused outright — the agent
    // cannot resolve the packet by asserting an identity through this flag.
    // (This is a protocol-shape guarantee, not a write-access guarantee: see
    // recordHumanApproval below and coordination-rules.md §Human Questions.)
    await expect(
      answerQuestionPacket(root, "feature-storage", answered, "guided", "forged-approval"),
    ).rejects.toThrow("no human-surface approval found");

    // recordHumanApproval stands in for CAO's real human-approval surface.
    // In this sample it is a JSON file under the agent's own write reach, so
    // the no-agent-CLI-subcommand-mints-this property holds, but a real
    // write-access boundary needs the record to live outside the agent's
    // reach or be signed with a key it doesn't hold.
    await recordHumanApproval(
      root,
      "feature-storage-approval",
      "feature-storage",
      "Portfolio owner",
    );

    // An altered question body is still rejected even with a valid approval.
    const altered = join(root, "altered-answers.md");
    await writeFile(
      altered,
      questionText
        .replace("## Storage", "## Storage and cache")
        .replace("[Answer]:", "[Answer]: A. SQL"),
    );
    await expect(
      answerQuestionPacket(
        root,
        "feature-storage",
        altered,
        "chat",
        "feature-storage-approval",
      ),
    ).rejects.toThrow("changed generated question text");

    // An approval minted for a DIFFERENT packet cannot resolve this one.
    await recordHumanApproval(
      root,
      "other-packet-approval",
      "other-packet",
      "Portfolio owner",
    );
    await expect(
      answerQuestionPacket(
        root,
        "feature-storage",
        answered,
        "guided",
        "other-packet-approval",
      ),
    ).rejects.toThrow("authorizes packet other-packet");

    const resolved = await answerQuestionPacket(
      root,
      "feature-storage",
      answered,
      "guided",
      "feature-storage-approval",
    );
    // The identity is derived from the verified approval record, never the caller.
    expect(resolved.answeredBy).toBe("Portfolio owner");

    // The approval is single-use: it cannot be replayed to resolve again.
    await expect(
      answerQuestionPacket(
        root,
        "feature-storage",
        answered,
        "guided",
        "feature-storage-approval",
      ),
    ).rejects.toThrow("already answered");

    expect(
      await readFile(
        join(root, "portfolio/questions/feature-storage.md"),
        "utf8",
      ),
    ).toBe(answeredText);
    const packetMeta = JSON.parse(
      await readFile(
        join(root, "portfolio/questions/feature-storage.json"),
        "utf8",
      ),
    );
    expect(packetMeta.answeredBy).toBe("Portfolio owner");
    expect(packetMeta.approvalId).toBe("feature-storage-approval");
    const stateActive = JSON.parse(
      await readFile(join(root, "portfolio/state.json"), "utf8"),
    );
    expect(stateActive.sessions["feature:api"].status).toBe("active");
    await updateSession(root, "feature", "api", "completed");
    const stateCompleted = JSON.parse(
      await readFile(join(root, "portfolio/state.json"), "utf8"),
    );
    expect(stateCompleted.activeIntent).toBeNull();
  }, 20_000);

  test("CLI requires approval-id even when answered-by is supplied", async () => {
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
    await confirmDefaultDiscovery(root);
    await advanceToDispatch(root);
    await updateSession(root, "feature", "api", "active", "runner-terminal");
    const generated = join(root, "generated-questions.md");
    const questionText = "## Storage\n\nA. SQL\nB. Object storage\n\n[Answer]:\n";
    await writeFile(generated, questionText);
    await submitQuestionPacket(
      root,
      "cli-storage",
      "api",
      "feature",
      "application-design",
      generated,
    );
    const answered = join(root, "human-answers.md");
    await writeFile(
      answered,
      questionText.replace("[Answer]:", "[Answer]: A. SQL"),
    );
    const args = [
      "question", "answer", "--root", root, "--id", "cli-storage",
      "--file", answered, "--mode", "guided",
    ];
    const packetPath = join(root, "portfolio/questions/cli-storage.json");
    const statePath = join(root, "portfolio/state.json");
    const pendingPacket = await readFile(packetPath, "utf8");
    const waitingState = await readFile(statePath, "utf8");

    for (const extraFlags of [[], ["--answered-by", "Someone"]]) {
      const result = await runCli([...args, ...extraFlags]);
      expect(result.exitCode).not.toBe(0);
      expect(JSON.parse(result.stderr)).toEqual({
        ok: false,
        error: "--approval-id is required",
      });
      expect(await readFile(packetPath, "utf8")).toBe(pendingPacket);
      expect(await readFile(statePath, "utf8")).toBe(waitingState);
      expect(
        await readFile(join(root, "portfolio/questions/cli-storage.md"), "utf8"),
      ).toBe(questionText);
    }

    await recordHumanApproval(
      root,
      "cli-approval",
      "cli-storage",
      "Portfolio owner",
    );
    const result = await runCli([...args, "--approval-id", "cli-approval"]);
    expect(result.exitCode).toBe(0);
    expect(JSON.parse(result.stdout)).toMatchObject({
      ok: true,
      status: "active",
      answeredBy: "Portfolio owner",
    });
    expect(JSON.parse(await readFile(packetPath, "utf8")).status).toBe(
      "answered",
    );
  }, 20_000);

  test("CLI rejects removed learning and memory commands", async () => {
    const root = await temporaryRoot();
    await initializeWorkspace(root, "sample-portfolio", "Sample Portfolio");
    for (const [namespace, command, error] of [
      ["learning", "approve", "unknown command: learning approve"],
      ["learning", "reconcile", "unknown command: learning reconcile"],
      ["memory", "refresh", "unexpected argument: refresh"],
      ["memory", "inspect", "unexpected argument: inspect"],
      ["memory", "clean", "unexpected argument: clean"],
    ] as const) {
      const result = await runCli([namespace, command, "--root", root]);
      expect(result.exitCode).not.toBe(0);
      expect(JSON.parse(result.stderr)).toEqual({ ok: false, error });
    }
  }, 20_000);

  test("rejects legacy learning proposals already stored on disk", async () => {
    const root = await temporaryRoot();
    await initializeWorkspace(root, "sample-portfolio", "Sample Portfolio");
    const source = await learningFile(root, "legacy-learning", {
      rule: "Keep review explicit.",
    });
    const proposal = await readFile(source, "utf8");
    const stored = join(root, "portfolio/learnings/legacy-learning.yaml");
    for (const legacyField of ["baseRevision: old-sha", "status: approved"]) {
      await writeFile(stored, `${proposal}${legacyField}\n`);
      await expect(listLearningProposals(root)).rejects.toThrow(
        "learning proposal schema validation failed",
      );
    }
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

  test("reviews durable learnings before promoting them into CAO memory", async () => {
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

    // Durable learnings no longer go through a canonical-file CAS. The runner
    // PROPOSES; the supervisor REVIEWS, then either promotes (recording that it
    // wrote the learning into CAO memory via memory_store) or rejects.
    const first = await learningFile(root, "first-rule", {
      rule: "Construct service clients lazily inside request handlers.",
    });
    const second = await learningFile(root, "second-rule", {
      rule: "Require a focused test for each new request handler.",
    });
    await registerLearningProposal(root, first);
    await registerLearningProposal(root, second);

    const promoted = await promoteLearningProposal(
      root,
      "first-rule",
      "Portfolio supervisor",
    );
    expect(promoted).toMatchObject({
      outcome: "promoted",
      scope: "project",
      memoryType: "feedback",
    });
    await rejectLearningProposal(root, "second-rule", "Not durable.");

    // Promotion is single-use review: a decided proposal cannot be re-promoted,
    // whether it was promoted or rejected.
    await expect(
      promoteLearningProposal(root, "first-rule", "Portfolio supervisor"),
    ).rejects.toThrow("is promoted");
    await expect(
      promoteLearningProposal(root, "second-rule", "Portfolio supervisor"),
    ).rejects.toThrow("is rejected");

    const proposals = await listLearningProposals(root, "api");
    const byId = Object.fromEntries(proposals.map((p) => [p.id, p]));
    expect(byId["first-rule"]?.status).toBe("promoted");
    expect(byId["first-rule"]?.promotedBy).toBe("Portfolio supervisor");
    expect(byId["first-rule"]?.memory).toMatchObject({
      scope: "project",
      memoryType: "feedback",
    });
    expect(byId["second-rule"]?.status).toBe("rejected");

    // A worktree project.md is local scratch and never blocks completion.
    await expect(
      updateSession(root, "feature", "api", "completed"),
    ).resolves.toBeUndefined();
  });

  test("refuses to promote a learning that is not reviewed by a named supervisor", async () => {
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
    const proposal = await learningFile(root, "needs-review", {
      heading: "Testing Posture",
      rule: "Require pytest coverage for every handler.",
    });
    await registerLearningProposal(root, proposal);

    await expect(
      promoteLearningProposal(root, "needs-review", "   "),
    ).rejects.toThrow("promoted-by must identify the supervisor");

    const result = await promoteLearningProposal(
      root,
      "needs-review",
      "Portfolio supervisor",
      "api/testing-posture",
    );
    expect(result.outcome).toBe("promoted");
    const [stored] = await listLearningProposals(root, "api", "promoted");
    expect(stored?.memory?.key).toBe("api/testing-posture");
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

async function runCli(args: string[]): Promise<{
  exitCode: number;
  stdout: string;
  stderr: string;
}> {
  const child = Bun.spawn([
    process.execPath,
    "skills/aidlc-portfolio/scripts/portfolio.ts",
    ...args,
  ], {
    cwd: join(import.meta.dir, ".."),
    stdout: "pipe",
    stderr: "pipe",
  });
  const [exitCode, stdout, stderr] = await Promise.all([
    child.exited,
    new Response(child.stdout).text(),
    new Response(child.stderr).text(),
  ]);
  return { exitCode, stdout, stderr };
}
