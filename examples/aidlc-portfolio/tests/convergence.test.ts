import { afterEach, describe, expect, test } from "bun:test";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { stringify } from "yaml";
import {
  checkConvergence,
  decideConvergenceRisk,
  submitChildResult,
} from "../skills/aidlc-portfolio/scripts/convergence.ts";
import {
  initializeWorkspace,
  registerDocument,
} from "../skills/aidlc-portfolio/scripts/lib.ts";

const temporaryRoots: string[] = [];
const DEPENDENCY_TYPES = [
  "runtime",
  "contract",
  "data",
  "build",
  "deployment",
  "operational",
  "business",
  "release",
] as const;

afterEach(async () => {
  await Promise.all(
    temporaryRoots.splice(0).map((root) =>
      rm(root, { recursive: true, force: true }),
    ),
  );
});

describe("cross-project convergence", () => {
  test("converges compatible results across every dependency type", async () => {
    const root = await createFixture(
      DEPENDENCY_TYPES.map((type) => ({
        id: `web-to-api-${type}`,
        type,
        source: "web",
        target: "api",
      })),
    );
    const dependencyIds = DEPENDENCY_TYPES.map((type) => `web-to-api-${type}`);
    await submitResult(root, "api", {
      components: ["api-service"],
      capabilities: ["checkout"],
      assumptions: dependencyIds,
      contracts: [
        {
          dependency: "web-to-api-contract",
          change: "compatible",
          evidence: ["OpenAPI compatibility check passed."],
        },
      ],
    });
    await submitResult(root, "web", {
      components: ["web-service"],
      capabilities: ["checkout"],
      assumptions: dependencyIds,
    });

    const report = await checkConvergence(root);
    expect(report.ok).toBe(true);
    expect(report.relationships.map((item) => item.type).sort()).toEqual(
      [...DEPENDENCY_TYPES].sort(),
    );
    expect(report.relationships.every((item) => item.status === "satisfied")).toBe(
      true,
    );
    expect(report.affected["feature:api"]).toEqual({
      upstream: [],
      downstream: ["web"],
    });
    expect(report.affected["feature:web"]).toEqual({
      upstream: ["api"],
      downstream: [],
    });
  });

  test("blocks breaking contracts until a revision-bound human decision", async () => {
    const root = await createFixture([
      {
        id: "web-to-api",
        type: "contract",
        source: "web",
        target: "api",
      },
    ]);
    await submitResult(root, "api", {
      assumptions: ["web-to-api"],
      contracts: [
        {
          dependency: "web-to-api",
          change: "breaking",
          evidence: ["Response field removed."],
        },
      ],
    });
    await submitResult(root, "web", { assumptions: ["web-to-api"] });

    let report = await checkConvergence(root);
    expect(report.ok).toBe(false);
    const risk = report.risks.find((item) => item.category === "contract-change")!;
    expect(risk.effectiveDisposition).toBe("blocked");

    await decideConvergenceRisk(
      root,
      risk.id,
      "accepted",
      "Architecture Owner",
      "Consumers will migrate in the coordinated release.",
    );
    report = await checkConvergence(root);
    expect(report.ok).toBe(true);
    expect(report.relationships[0]?.status).toBe("deferred");

    await submitResult(root, "api", {
      assumptions: ["web-to-api"],
      contracts: [
        {
          dependency: "web-to-api",
          change: "unknown",
          evidence: ["Compatibility evidence is incomplete."],
        },
      ],
    });
    report = await checkConvergence(root);
    expect(report.ok).toBe(false);
    expect(
      report.risks.find((item) => item.category === "contract-change")?.decision,
    ).toBeNull();
  });

  test("enforces dependency ordering and refuses to accept remediable blockers", async () => {
    const root = await createFixture([
      {
        id: "web-to-api-release",
        type: "release",
        source: "web",
        target: "api",
        blockingAt: ["integration"],
      },
    ]);
    await setSessionStatus(root, "api", "active");
    await submitResult(root, "api", { assumptions: ["web-to-api-release"] });
    await submitResult(root, "web", { assumptions: ["web-to-api-release"] });

    let report = await checkConvergence(root);
    expect(report.ok).toBe(false);
    const orderingRisks = report.risks.filter(
      (item) => item.category === "dependency-order",
    );
    expect(orderingRisks).toHaveLength(1);
    expect(orderingRisks[0]?.project).toBe("web");
    const ordering = orderingRisks[0]!;
    await expect(
      decideConvergenceRisk(
        root,
        ordering.id,
        "accepted",
        "Release Owner",
        "Attempt to bypass ordering.",
      ),
    ).rejects.toThrow("requires remediation");

    await setSessionStatus(root, "api", "completed");
    report = await checkConvergence(root);
    expect(report.ok).toBe(true);
  });

  test("traverses cyclic project relationships without duplication", async () => {
    const root = await createFixture([
      {
        id: "api-to-web",
        type: "runtime",
        source: "api",
        target: "web",
      },
      {
        id: "web-to-api",
        type: "data",
        source: "web",
        target: "api",
      },
    ]);
    await submitResult(root, "api", {
      assumptions: ["api-to-web", "web-to-api"],
    });
    await submitResult(root, "web", {
      assumptions: ["api-to-web", "web-to-api"],
    });

    const report = await checkConvergence(root);
    expect(report.ok).toBe(true);
    expect(report.affected["feature:api"]).toEqual({
      upstream: ["web"],
      downstream: ["web"],
    });
    expect(report.affected["feature:web"]).toEqual({
      upstream: ["api"],
      downstream: ["api"],
    });
  });

  test("requires every repository result and explicit acceptance of deferred risk", async () => {
    const root = await createFixture([
      {
        id: "web-to-api-data",
        type: "data",
        source: "web",
        target: "api",
      },
    ]);
    await submitResult(root, "api", { assumptions: ["web-to-api-data"] });

    let report = await checkConvergence(root);
    expect(report.ok).toBe(false);
    expect(report.risks.some((item) => item.category === "missing-result")).toBe(
      true,
    );
    expect(report.affected["feature:web"]).toEqual({
      upstream: ["api"],
      downstream: [],
    });
    expect(report.relationships[0]?.status).toBe("blocked");

    await submitResult(root, "web", {
      assumptionDisposition: "deferred",
      assumptions: ["web-to-api-data"],
    });
    report = await checkConvergence(root);
    const deferred = report.risks.find(
      (item) =>
        item.project === "web" && item.category === "dependency-assumption",
    )!;
    expect(report.ok).toBe(false);
    await decideConvergenceRisk(
      root,
      deferred.id,
      "accepted",
      "Portfolio Owner",
      "Data migration validation is accepted as a post-release action.",
    );
    expect((await checkConvergence(root)).ok).toBe(true);
  });
});

async function createFixture(
  dependencies: Array<{
    id: string;
    type: (typeof DEPENDENCY_TYPES)[number];
    source: "api" | "web";
    target: "api" | "web";
    blockingAt?: string[];
  }>,
): Promise<string> {
  const root = await mkdtemp(join(tmpdir(), "aidlc-portfolio-convergence-"));
  temporaryRoots.push(root);
  await initializeWorkspace(root, "sample-portfolio", "Sample Portfolio");
  await registerProject(root, "api");
  await registerProject(root, "web");
  for (const dependency of dependencies) {
    await registerDocument(
      root,
      "dependency",
      await writeYaml(root, `${dependency.id}.yaml`, {
        schemaVersion: 1,
        id: dependency.id,
        source: {
          project: dependency.source,
          component: `${dependency.source}-service`,
        },
        target: {
          project: dependency.target,
          component: `${dependency.target}-service`,
        },
        type: dependency.type,
        blockingAt: dependency.blockingAt ?? ["integration"],
        status: "verified",
        confidence: "high",
        evidence: [{ source: "test" }],
        lastVerified: "2026-07-27",
      }),
    );
  }
  await registerDocument(
    root,
    "intent",
    await writeYaml(root, "feature.yaml", {
      schemaVersion: 1,
      id: "feature",
      name: "Feature",
      objective: "Deliver a cross-project feature.",
      status: "active",
      businessOutcomes: ["Improve checkout"],
      projects: [
        {
          project: "api",
          aidlcIntent: "feature-api",
          aidlcSpace: "default",
          branch: "feat/feature-api",
          worktree: "worktrees/api/feature",
          dependsOn: [],
        },
        {
          project: "web",
          aidlcIntent: "feature-web",
          aidlcSpace: "default",
          branch: "feat/feature-web",
          worktree: "worktrees/web/feature",
          dependsOn: [],
        },
      ],
      createdAt: "2026-07-27T00:00:00.000Z",
      updatedAt: "2026-07-27T00:00:00.000Z",
    }),
  );
  await setSessionStatus(root, "api", "completed");
  await setSessionStatus(root, "web", "completed");
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
      status: "verified",
      businessCapabilities: ["checkout"],
      components: [
        {
          id: `${id}-service`,
          name: `${id} service`,
          type: "service",
          criticality: "tier-2",
        },
      ],
      owners: { team: `${id}-team` },
      environments: ["production"],
      evidence: [{ source: "test" }],
      confidence: "high",
      lastVerified: "2026-07-27",
    }),
  );
}

async function submitResult(
  root: string,
  project: "api" | "web",
  options: {
    components?: string[];
    capabilities?: string[];
    assumptions?: string[];
    assumptionDisposition?: "satisfied" | "blocked" | "deferred" | "unknown";
    contracts?: Array<{
      dependency: string;
      change: "compatible" | "breaking" | "unknown";
      evidence: string[];
    }>;
  },
): Promise<void> {
  const file = await writeYaml(root, `${project}-result.yaml`, {
    schemaVersion: 1,
    project,
    intent: "feature",
    changedComponents: options.components ?? [],
    changedCapabilities: options.capabilities ?? [],
    contracts: options.contracts ?? [],
    dependencyAssumptions: (options.assumptions ?? []).map((dependency) => ({
      dependency,
      disposition: options.assumptionDisposition ?? "satisfied",
      note:
        options.assumptionDisposition === "deferred"
          ? "Validation is deferred."
          : "Dependency is satisfied.",
    })),
    verification: ["Child verification passed."],
    submittedAt: "2026-07-27T00:00:00.000Z",
  });
  await submitChildResult(root, file);
}

async function setSessionStatus(
  root: string,
  project: string,
  status: string,
): Promise<void> {
  const path = join(root, "portfolio/state.json");
  const state = JSON.parse(await readFile(path, "utf8")) as {
    sessions: Record<string, { status: string }>;
  };
  state.sessions[`feature:${project}`]!.status = status;
  await writeFile(path, `${JSON.stringify(state, null, 2)}\n`);
}

async function writeYaml(
  root: string,
  name: string,
  value: unknown,
): Promise<string> {
  const path = join(root, name);
  await writeFile(path, stringify(value));
  return path;
}
