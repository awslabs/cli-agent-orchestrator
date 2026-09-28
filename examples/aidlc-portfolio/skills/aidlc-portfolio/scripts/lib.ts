import Ajv2020, { type ErrorObject, type ValidateFunction } from "ajv/dist/2020.js";
import { createHash } from "node:crypto";
import {
  access,
  mkdir,
  readFile,
  readdir,
  rename,
  rm,
  stat,
  writeFile,
} from "node:fs/promises";
import { basename, dirname, join, relative, resolve, sep } from "node:path";
import { parse, stringify } from "yaml";
import { checkConvergence } from "./convergence.ts";
import { verifyHarness } from "./harness.ts";

export type DocumentKind = "project" | "dependency" | "intent";
export type MemoryDestination = "project" | "team";
export type LearningStatus = "pending" | "applied" | "rejected";
export type QuestionAnswerMode = "guided" | "markdown" | "chat";
export type PortfolioPhase =
  | "bootstrap"
  | "discover"
  | "confirm"
  | "plan"
  | "dispatch"
  | "integrate"
  | "learn";
export type SessionStatus =
  | "pending"
  | "active"
  | "waiting"
  | "blocked"
  | "completed"
  | "failed";

export interface ValidationResult {
  ok: boolean;
  errors: string[];
  counts: {
    projects: number;
    dependencies: number;
    intents: number;
  };
}

interface PortfolioDocument {
  schemaVersion: 1;
  id: string;
  name: string;
  status: "draft" | "active" | "archived";
  organization: { name: string; domains: string[] };
  businessCapabilities: string[];
  createdAt: string;
  updatedAt: string;
}

interface ProjectDocument {
  id: string;
  repository: string;
  components: Array<{ id: string }>;
}

interface DependencyDocument {
  id: string;
  type: string;
  status: string;
  source: { project: string; component?: string };
  target: { project: string; component?: string };
  blockingAt: string[];
}

interface IntentProject {
  project: string;
  aidlcIntent: string;
  aidlcSpace?: string;
  branch: string;
  worktree: string;
  dependsOn: string[];
}

interface IntentDocument {
  id: string;
  status: string;
  projects: IntentProject[];
}

interface SessionRecord {
  status: SessionStatus;
  terminalId: string | null;
  updatedAt: string;
}

interface PortfolioState {
  schemaVersion: 2;
  portfolioId: string;
  activeIntent: string | null;
  sessions: Record<string, SessionRecord>;
  lifecycle: {
    status: "running" | "completed";
    phase: PortfolioPhase;
    history: Array<{
      from: PortfolioPhase | null;
      to: PortfolioPhase;
      at: string;
      actor: string;
      note?: string;
    }>;
    planAcceptance: {
      acceptedBy: string;
      acceptedAt: string;
      catalogRevision: string;
    } | null;
    completedAt: string | null;
  };
  updatedAt: string;
}

interface LegacyPortfolioState {
  schemaVersion: 1;
  portfolioId: string;
  activeIntent: string | null;
  sessions: Record<string, SessionRecord>;
  updatedAt: string;
}

type DiscoveryDisposition = "confirmed" | "unknown" | "deferred";
type DiscoveryFactName =
  | "organization"
  | "businessOutcomes"
  | "businessCapabilities"
  | "dependencies";

interface DiscoveryDecision {
  schemaVersion: 1;
  organization: {
    disposition: DiscoveryDisposition;
    value: string;
  };
  businessOutcomes: {
    disposition: DiscoveryDisposition;
    values: string[];
  };
  businessCapabilities: {
    disposition: DiscoveryDisposition;
    values: string[];
  };
  dependencies: {
    disposition: DiscoveryDisposition;
    ids: string[];
  };
  acceptance: {
    acceptedBy: string;
    acceptedAt: string;
    unknowns: DiscoveryFactName[];
    deferrals: DiscoveryFactName[];
  };
  catalogRevision?: string;
}

interface QuestionPacket {
  schemaVersion: 1;
  id: string;
  project: string;
  intent: string;
  stage: string;
  status: "pending" | "answered";
  questionRevision: string;
  questionCount: number;
  createdAt: string;
  answeredAt?: string;
  answerMode?: QuestionAnswerMode;
  answeredBy?: string;
}

interface LoadedWorkspace {
  root: string;
  portfolio: PortfolioDocument;
  state: PortfolioState;
  projects: ProjectDocument[];
  dependencies: DependencyDocument[];
  intents: IntentDocument[];
}

interface LearningProposal {
  schemaVersion: 1;
  id: string;
  project: string;
  intent: string;
  space: string;
  destination: MemoryDestination;
  heading: string;
  rule: string;
  evidence: string[];
  source: { stage: string };
  baseRevision: string;
  status?: LearningStatus;
  createdAt?: string;
  updatedAt?: string;
  appliedAt?: string;
  rejectedAt?: string;
  rejectionReason?: string;
  reconciliations?: Array<{
    note: string;
    reconciledAt: string;
    previousRevision: string;
  }>;
}

export interface MemorySnapshot {
  project: string;
  intent: string;
  space: string;
  destination: MemoryDestination;
  canonicalPath: string;
  worktreePath: string;
  canonicalRevision: string;
  worktreeRevision: string;
  inSync: boolean;
  headRevision: string | null;
  mergeClean: boolean | null;
}

interface DispatchReadiness {
  worktree: string;
  branch: string;
  aidlcIntent: string;
  aidlcSpace: string;
  memory: Record<MemoryDestination, { revision: string; path: string }>;
}

interface PortfolioLockOwner {
  token: string;
  pid: number;
  createdAt: string;
}

const SCHEMA_FILES: Record<DocumentKind | "portfolio", string> = {
  portfolio: "portfolio.schema.json",
  project: "project.schema.json",
  dependency: "dependency.schema.json",
  intent: "intent.schema.json",
};

const REQUIRED_DIRECTORIES = [
  "portfolio/projects",
  "portfolio/dependencies",
  "portfolio/contracts",
  "portfolio/intents",
  "portfolio/learnings",
  "portfolio/questions",
  "portfolio/results",
  "portfolio/convergence-decisions",
  "repositories",
  "worktrees",
];

const SESSION_STATUSES: SessionStatus[] = [
  "pending",
  "active",
  "waiting",
  "blocked",
  "completed",
  "failed",
];

const PORTFOLIO_PHASES: PortfolioPhase[] = [
  "bootstrap",
  "discover",
  "confirm",
  "plan",
  "dispatch",
  "integrate",
  "learn",
];

const NEXT_PHASE: Record<PortfolioPhase, PortfolioPhase | null> = {
  bootstrap: "discover",
  discover: "confirm",
  confirm: "plan",
  plan: "dispatch",
  dispatch: "integrate",
  integrate: "learn",
  learn: null,
};

const LOCK_OWNER_GRACE_MS = 30_000;

export class PortfolioError extends Error {}

export function workspacePaths(rootInput: string) {
  const root = resolve(rootInput);
  return {
    root,
    portfolio: join(root, "portfolio"),
    portfolioFile: join(root, "portfolio", "portfolio.yaml"),
    stateFile: join(root, "portfolio", "state.json"),
    projects: join(root, "portfolio", "projects"),
    dependencies: join(root, "portfolio", "dependencies"),
    contracts: join(root, "portfolio", "contracts"),
    intents: join(root, "portfolio", "intents"),
    learnings: join(root, "portfolio", "learnings"),
    questions: join(root, "portfolio", "questions"),
    results: join(root, "portfolio", "results"),
    convergenceDecisions: join(
      root,
      "portfolio",
      "convergence-decisions",
    ),
    discoveryFile: join(root, "portfolio", "discovery.yaml"),
    repositories: join(root, "repositories"),
    worktrees: join(root, "worktrees"),
  };
}

export function sessionKey(intent: string, project: string): string {
  return `${intent}:${project}`;
}

export async function pathExists(path: string): Promise<boolean> {
  try {
    await access(path);
    return true;
  } catch {
    return false;
  }
}

export function assertId(value: string, label = "id"): void {
  if (!/^[a-z0-9][a-z0-9-]*$/.test(value)) {
    throw new PortfolioError(`${label} must use lowercase letters, digits, and hyphens`);
  }
}

export async function initializeWorkspace(
  rootInput: string,
  id: string,
  name: string,
): Promise<{ created: boolean; root: string }> {
  assertId(id, "portfolio id");
  if (!name.trim()) {
    throw new PortfolioError("portfolio name must not be empty");
  }

  const paths = workspacePaths(rootInput);
  await mkdir(paths.portfolio, { recursive: true });

  return withLock(paths.root, async () => {
    if (await pathExists(paths.portfolioFile)) {
      const existing = await readYaml<PortfolioDocument>(paths.portfolioFile);
      if (existing.id !== id || existing.name !== name) {
        throw new PortfolioError(
          `workspace already belongs to portfolio ${existing.id} (${existing.name})`,
        );
      }
      await ensureDirectories(paths.root);
      const validation = await validateWorkspace(paths.root);
      if (!validation.ok) {
        throw new PortfolioError(validation.errors.join("\n"));
      }
      return { created: false, root: paths.root };
    }

    await ensureDirectories(paths.root);
    const now = new Date().toISOString();
    const portfolio: PortfolioDocument = {
      schemaVersion: 1,
      id,
      name,
      status: "draft",
      organization: { name: "Unknown", domains: [] },
      businessCapabilities: [],
      createdAt: now,
      updatedAt: now,
    };
    const state: PortfolioState = {
      schemaVersion: 2,
      portfolioId: id,
      activeIntent: null,
      sessions: {},
      lifecycle: {
        status: "running",
        phase: "bootstrap",
        history: [
          {
            from: null,
            to: "bootstrap",
            at: now,
            actor: "init",
          },
        ],
        planAcceptance: null,
        completedAt: null,
      },
      updatedAt: now,
    };

    await atomicWrite(paths.portfolioFile, stringify(portfolio));
    await atomicWrite(paths.stateFile, `${JSON.stringify(state, null, 2)}\n`);
    const questions = await readFile(
      new URL("../assets/templates/portfolio-questions.md", import.meta.url),
      "utf8",
    );
    await atomicWrite(join(paths.questions, "portfolio-discovery.md"), questions);

    const validation = await validateWorkspace(paths.root);
    if (!validation.ok) {
      throw new PortfolioError(validation.errors.join("\n"));
    }
    return { created: true, root: paths.root };
  });
}

export async function registerDocument(
  rootInput: string,
  kind: DocumentKind,
  sourceFile: string,
): Promise<string> {
  const root = resolve(rootInput);
  const document = await readYaml<Record<string, unknown>>(resolve(sourceFile));
  const id = String(document.id ?? "");
  assertId(id, `${kind} id`);

  const validate = await schemaValidator(kind);
  if (!validate(document)) {
    throw new PortfolioError(formatSchemaErrors(kind, validate.errors));
  }

  return withLock(root, async () => {
    const paths = workspacePaths(root);
    const targetDirectory =
      kind === "project"
        ? paths.projects
        : kind === "dependency"
          ? paths.dependencies
          : paths.intents;
    const target = join(targetDirectory, `${id}.yaml`);
    const previousTarget = await optionalRead(target);
    const previousState = kind === "intent" ? await optionalRead(paths.stateFile) : null;

    try {
      await atomicWrite(target, stringify(document));
      if (kind === "intent") {
        await ensureIntentSessions(root, document as unknown as IntentDocument);
      }

      const validation = await validateWorkspace(root);
      if (!validation.ok) {
        throw new PortfolioError(validation.errors.join("\n"));
      }
      return target;
    } catch (error) {
      await restoreFile(target, previousTarget);
      if (kind === "intent") {
        await restoreFile(paths.stateFile, previousState);
      }
      throw error;
    }
  });
}

export async function validateWorkspace(rootInput: string): Promise<ValidationResult> {
  const root = resolve(rootInput);
  const errors: string[] = [];
  const counts = { projects: 0, dependencies: 0, intents: 0 };

  try {
    const loaded = await loadWorkspace(root);
    counts.projects = loaded.projects.length;
    counts.dependencies = loaded.dependencies.length;
    counts.intents = loaded.intents.length;

    const projectMap = new Map(loaded.projects.map((project) => [project.id, project]));
    validateState(loaded, errors);
    validateProjectPaths(loaded, errors);
    validateDependencies(loaded, projectMap, errors);
    validateIntents(loaded, projectMap, errors);
    const learnings = await readLearningProposals(root);
    for (const proposal of learnings) {
      try {
        resolveMemoryPaths(
          loaded,
          proposal.project,
          proposal.intent,
          proposal.destination,
          proposal.space,
        );
      } catch (error) {
        errors.push(`learning proposal ${proposal.id}: ${errorMessage(error)}`);
      }
    }
  } catch (error) {
    errors.push(errorMessage(error));
  }

  return { ok: errors.length === 0, errors, counts };
}

export async function doctorWorkspace(rootInput: string): Promise<ValidationResult> {
  const root = resolve(rootInput);
  const result = await validateWorkspace(root);
  for (const directory of REQUIRED_DIRECTORIES) {
    const fullPath = join(root, directory);
    if (!(await pathExists(fullPath))) {
      result.errors.push(`missing required directory: ${fullPath}`);
    }
  }
  result.ok = result.errors.length === 0;
  return result;
}

export async function portfolioStatus(rootInput: string) {
  const loaded = await loadWorkspace(resolve(rootInput));
  const learnings = await readLearningProposals(loaded.root);
  const questions = await listQuestionPackets(loaded.root);
  const sessions = Object.entries(loaded.state.sessions).map(([key, value]) => ({
    key,
    ...value,
  }));
  return {
    portfolio: {
      id: loaded.portfolio.id,
      name: loaded.portfolio.name,
      status: loaded.portfolio.status,
    },
    activeIntent: loaded.state.activeIntent,
    lifecycle: await lifecycleSummary(loaded),
    counts: {
      projects: loaded.projects.length,
      dependencies: loaded.dependencies.length,
      intents: loaded.intents.length,
      learnings: {
        pending: learnings.filter((item) => item.status === "pending").length,
        applied: learnings.filter((item) => item.status === "applied").length,
        rejected: learnings.filter((item) => item.status === "rejected").length,
      },
      questions: {
        pending: questions.filter((item) => item.status === "pending").length,
        answered: questions.filter((item) => item.status === "answered").length,
      },
    },
    sessions,
  };
}

export async function migrateLifecycle(
  rootInput: string,
): Promise<{ migrated: boolean; phase: PortfolioPhase }> {
  const root = resolve(rootInput);
  return withLock(root, async () => {
    const path = workspacePaths(root).stateFile;
    const state = await readJson<PortfolioState | LegacyPortfolioState>(path);
    if (state.schemaVersion === 2) {
      return { migrated: false, phase: state.lifecycle.phase };
    }
    if (state.schemaVersion !== 1) {
      throw new PortfolioError(
        `unsupported state.json schemaVersion: ${String(
          (state as { schemaVersion?: unknown }).schemaVersion,
        )}`,
      );
    }

    const now = new Date().toISOString();
    const phase = await inferMigrationPhase(root, state);
    const migrated: PortfolioState = {
      ...state,
      schemaVersion: 2,
      lifecycle: {
        status: "running",
        phase,
        history: [
          {
            from: null,
            to: phase,
            at: now,
            actor: "migration",
            note: "Migrated from portfolio state schemaVersion 1.",
          },
        ],
        planAcceptance: null,
        completedAt: null,
      },
      updatedAt: now,
    };
    await atomicWrite(path, `${JSON.stringify(migrated, null, 2)}\n`);
    return { migrated: true, phase };
  });
}

export async function lifecycleStatus(rootInput: string) {
  const loaded = await loadWorkspace(resolve(rootInput));
  return lifecycleSummary(loaded);
}

export async function advanceLifecycle(
  rootInput: string,
  targetInput: string,
  actor?: string,
): Promise<{
  changed: boolean;
  phase: PortfolioPhase;
  status: "running" | "completed";
}> {
  const target = requirePortfolioPhase(targetInput);
  const root = resolve(rootInput);
  return withLock(root, async () => {
    const loaded = await loadWorkspace(root);
    const lifecycle = loaded.state.lifecycle;
    if (lifecycle.status === "completed") {
      throw new PortfolioError("portfolio lifecycle is already completed");
    }
    if (target === lifecycle.phase) {
      if (target === "dispatch" && actor?.trim()) {
        const blockers = await transitionBlockers(root, loaded, target, actor);
        if (blockers.length > 0) {
          throw new PortfolioError(
            `cannot accept the current dispatch plan:\n${blockers
              .map((blocker) => `- ${blocker}`)
              .join("\n")}`,
          );
        }
        const discovery = await requireConfirmedDiscovery(root, loaded);
        const acceptance = lifecycle.planAcceptance;
        const alreadyCurrent =
          acceptance?.catalogRevision === discovery.catalogRevision &&
          acceptance?.acceptedBy === actor.trim();
        if (alreadyCurrent) {
          return {
            changed: false,
            phase: lifecycle.phase,
            status: lifecycle.status,
          };
        }
        const now = new Date().toISOString();
        lifecycle.planAcceptance = {
          acceptedBy: actor.trim(),
          acceptedAt: now,
          catalogRevision: discovery.catalogRevision!,
        };
        lifecycle.history.push({
          from: "dispatch",
          to: "dispatch",
          at: now,
          actor: actor.trim(),
          note: "Accepted the current catalog revision for dispatch.",
        });
        loaded.state.updatedAt = now;
        await writePortfolioState(root, loaded.state);
        return { changed: true, phase: "dispatch", status: lifecycle.status };
      }
      return { changed: false, phase: lifecycle.phase, status: lifecycle.status };
    }
    const expected = NEXT_PHASE[lifecycle.phase];
    if (target !== expected) {
      throw new PortfolioError(
        `cannot advance lifecycle from ${lifecycle.phase} to ${target}; next phase is ${
          expected ?? "completion"
        }`,
      );
    }

    const blockers = await transitionBlockers(root, loaded, target, actor);
    if (blockers.length > 0) {
      throw new PortfolioError(
        `cannot advance lifecycle from ${lifecycle.phase} to ${target}:\n${blockers
          .map((blocker) => `- ${blocker}`)
          .join("\n")}`,
      );
    }

    const now = new Date().toISOString();
    if (target === "dispatch") {
      const discovery = await requireConfirmedDiscovery(root, loaded);
      lifecycle.planAcceptance = {
        acceptedBy: actor!.trim(),
        acceptedAt: now,
        catalogRevision: discovery.catalogRevision!,
      };
    }
    lifecycle.history.push({
      from: lifecycle.phase,
      to: target,
      at: now,
      actor: actor?.trim() || "portfolio-supervisor",
    });
    lifecycle.phase = target;
    loaded.state.updatedAt = now;
    await writePortfolioState(root, loaded.state);
    return { changed: true, phase: target, status: lifecycle.status };
  });
}

export async function completeLifecycle(
  rootInput: string,
  actor?: string,
): Promise<{
  changed: boolean;
  phase: PortfolioPhase;
  status: "completed";
}> {
  const root = resolve(rootInput);
  return withLock(root, async () => {
    const loaded = await loadWorkspace(root);
    const lifecycle = loaded.state.lifecycle;
    if (lifecycle.status === "completed") {
      return { changed: false, phase: lifecycle.phase, status: "completed" };
    }
    if (lifecycle.phase !== "learn") {
      throw new PortfolioError(
        `cannot complete lifecycle from ${lifecycle.phase}; advance to learn first`,
      );
    }
    const blockers = await integrationBlockers(root, loaded);
    if (blockers.length > 0) {
      throw new PortfolioError(
        `cannot complete portfolio lifecycle:\n${blockers
          .map((blocker) => `- ${blocker}`)
          .join("\n")}`,
      );
    }
    const now = new Date().toISOString();
    lifecycle.status = "completed";
    lifecycle.completedAt = now;
    lifecycle.history.push({
      from: "learn",
      to: "learn",
      at: now,
      actor: actor?.trim() || "portfolio-supervisor",
      note: "Portfolio lifecycle completed.",
    });
    loaded.state.updatedAt = now;
    await writePortfolioState(root, loaded.state);
    return { changed: true, phase: "learn", status: "completed" };
  });
}

export async function confirmDiscovery(
  rootInput: string,
  sourceFile: string,
): Promise<{ path: string; catalogRevision: string }> {
  const root = resolve(rootInput);
  const input = await readYaml<DiscoveryDecision>(resolve(sourceFile));
  await validateDiscoveryDecision(input);

  return withLock(root, async () => {
    const loaded = await loadWorkspace(root);
    validateDiscoveryAcceptance(input, loaded);

    const now = new Date().toISOString();
    loaded.portfolio.organization.name =
      input.organization.disposition === "confirmed"
        ? input.organization.value.trim()
        : "Unknown";
    loaded.portfolio.businessCapabilities =
      input.businessCapabilities.disposition === "confirmed"
        ? [...input.businessCapabilities.values]
        : [];
    loaded.portfolio.updatedAt = now;
    await atomicWrite(
      workspacePaths(root).portfolioFile,
      stringify(loaded.portfolio),
    );

    const refreshed = await loadWorkspace(root);
    const catalogRevision = catalogRevisionFor(refreshed);
    const decision: DiscoveryDecision = { ...input, catalogRevision };
    await atomicWrite(
      workspacePaths(root).discoveryFile,
      stringify(decision),
    );
    return { path: workspacePaths(root).discoveryFile, catalogRevision };
  });
}

export async function submitQuestionPacket(
  rootInput: string,
  id: string,
  projectId: string,
  intentId: string,
  stage: string,
  sourceFile: string,
): Promise<{ path: string; status: "waiting" }> {
  assertId(id, "question packet id");
  assertId(stage, "stage");
  const root = resolve(rootInput);
  const content = await readFile(resolve(sourceFile), "utf8");
  const parsed = parseQuestionDocument(content);
  if (parsed.answers.some((answer) => answer.trim().length > 0)) {
    throw new PortfolioError(
      "generated question packets must not contain answers",
    );
  }

  return withLock(root, async () => {
    const loaded = await loadWorkspace(root);
    requireIntentProject(loaded, intentId, projectId);
    requireSessionStatus(loaded, intentId, projectId, "active");
    const metadataPath = questionMetadataPath(root, id);
    if (await pathExists(metadataPath)) {
      throw new PortfolioError(`question packet already exists: ${id}`);
    }

    const now = new Date().toISOString();
    const packet: QuestionPacket = {
      schemaVersion: 1,
      id,
      project: projectId,
      intent: intentId,
      stage,
      status: "pending",
      questionRevision: contentRevision(parsed.skeleton),
      questionCount: parsed.answers.length,
      createdAt: now,
    };
    await atomicWrite(questionDocumentPath(root, id), content);
    await atomicWrite(metadataPath, `${JSON.stringify(packet, null, 2)}\n`);
    await setSessionStatus(loaded, intentId, projectId, "waiting");
    return { path: questionDocumentPath(root, id), status: "waiting" };
  });
}

export async function answerQuestionPacket(
  rootInput: string,
  id: string,
  sourceFile: string,
  mode: QuestionAnswerMode,
  answeredBy: string,
): Promise<{ path: string; status: "active" }> {
  assertId(id, "question packet id");
  if (!["guided", "markdown", "chat"].includes(mode)) {
    throw new PortfolioError(`invalid question answer mode: ${mode}`);
  }
  if (!answeredBy.trim()) {
    throw new PortfolioError("answered-by must identify the human decision maker");
  }
  const root = resolve(rootInput);
  const content = await readFile(resolve(sourceFile), "utf8");
  const parsed = parseQuestionDocument(content);

  return withLock(root, async () => {
    const packet = await readQuestionPacket(root, id);
    if (packet.status !== "pending") {
      throw new PortfolioError(`question packet ${id} is already ${packet.status}`);
    }
    if (
      parsed.answers.length !== packet.questionCount ||
      contentRevision(parsed.skeleton) !== packet.questionRevision
    ) {
      throw new PortfolioError(
        "answered question packet changed generated question text or structure",
      );
    }
    if (parsed.answers.some((answer) => answer.trim().length === 0)) {
      throw new PortfolioError("every generated question requires a human answer");
    }

    const loaded = await loadWorkspace(root);
    requireIntentProject(loaded, packet.intent, packet.project);
    requireSessionStatus(loaded, packet.intent, packet.project, "waiting");
    const now = new Date().toISOString();
    const answered: QuestionPacket = {
      ...packet,
      status: "answered",
      answeredAt: now,
      answerMode: mode,
      answeredBy: answeredBy.trim(),
    };
    await atomicWrite(questionDocumentPath(root, id), content);
    await atomicWrite(
      questionMetadataPath(root, id),
      `${JSON.stringify(answered, null, 2)}\n`,
    );
    await setSessionStatus(loaded, packet.intent, packet.project, "active");
    return { path: questionDocumentPath(root, id), status: "active" };
  });
}

export async function listQuestionPackets(
  rootInput: string,
  projectId?: string,
  intentId?: string,
  status?: QuestionPacket["status"],
): Promise<QuestionPacket[]> {
  const root = resolve(rootInput);
  const directory = workspacePaths(root).questions;
  if (!(await pathExists(directory))) return [];
  const names = (await readdir(directory))
    .filter((name) => name.endsWith(".json"))
    .sort();
  const packets = await Promise.all(
    names.map((name) => readJson<QuestionPacket>(join(directory, name))),
  );
  return packets.filter(
    (packet) =>
      (!projectId || packet.project === projectId) &&
      (!intentId || packet.intent === intentId) &&
      (!status || packet.status === status),
  );
}

export async function updateSession(
  rootInput: string,
  intentId: string,
  projectId: string,
  status: SessionStatus,
  terminalId?: string,
): Promise<void> {
  const root = resolve(rootInput);
  await withLock(root, async () => {
    const loaded = await loadWorkspace(root);
    const intent = loaded.intents.find((item) => item.id === intentId);
    const mapping = intent?.projects.find((item) => item.project === projectId);
    if (!mapping) {
      throw new PortfolioError(`intent ${intentId} does not contain project ${projectId}`);
    }
    if (status === "active") {
      await assertDispatchable(root, loaded, mapping, projectId, intentId);
    }
    if (status === "completed") {
      const pendingQuestions = await listQuestionPackets(
        root,
        projectId,
        intentId,
        "pending",
      );
      if (pendingQuestions.length > 0) {
        throw new PortfolioError(
          `session ${intentId}:${projectId} has unanswered human questions: ${pendingQuestions
            .map((packet) => packet.id)
            .join(", ")}`,
        );
      }
    }
    if (status === "completed") {
      for (const destination of ["project", "team"] as const) {
        const { canonicalPath, relativeMemory, worktreePath } = resolveMemoryPaths(
          loaded,
          projectId,
          intentId,
          destination,
          mapping.aidlcSpace ?? "default",
        );
        const canonical = contentRevision((await optionalRead(canonicalPath)) ?? "");
        const worktree = contentRevision((await optionalRead(worktreePath)) ?? "");
        const head = await readHeadMemory(dirnameFromRelative(worktreePath, relativeMemory), relativeMemory);
        const mergeClean = head ? contentRevision(head.content) === worktree : null;
        if (mergeClean === false || (mergeClean === null && canonical !== worktree)) {
          throw new PortfolioError(
            `${destination}.md contains worktree changes; submit proposals and clean memory before completing the session`,
          );
        }
      }
    }

    const key = sessionKey(intentId, projectId);
    const previous = loaded.state.sessions[key];
    loaded.state.sessions[key] = {
      status,
      terminalId: terminalId ?? previous?.terminalId ?? null,
      updatedAt: new Date().toISOString(),
    };
    loaded.state.activeIntent = activeIntentFrom(loaded.state.sessions);
    loaded.state.updatedAt = new Date().toISOString();
    await atomicWrite(
      workspacePaths(root).stateFile,
      `${JSON.stringify(loaded.state, null, 2)}\n`,
    );
  });
}

export async function createWorktree(
  rootInput: string,
  projectId: string,
  intentId: string,
  branch: string,
  base: string,
): Promise<string> {
  const root = resolve(rootInput);
  return withLock(root, async () => {
    const loaded = await loadWorkspace(root);
    const project = loaded.projects.find((item) => item.id === projectId);
    const intent = loaded.intents.find((item) => item.id === intentId);
    const mapping = intent?.projects.find((item) => item.project === projectId);
    if (!project || !intent || !mapping) {
      throw new PortfolioError(
        `project ${projectId} is not registered in intent ${intentId}`,
      );
    }
    if (mapping.branch !== branch) {
      throw new PortfolioError(`branch must match intent mapping: ${mapping.branch}`);
    }

    const repository = resolveInside(
      root,
      project.repository,
      workspacePaths(root).repositories,
    );
    const worktree = resolveInside(
      root,
      mapping.worktree,
      workspacePaths(root).worktrees,
    );
    if (!(await pathExists(repository))) {
      throw new PortfolioError(`repository does not exist: ${repository}`);
    }
    if (await pathExists(worktree)) {
      throw new PortfolioError(`worktree already exists: ${worktree}`);
    }

    const parent = dirname(worktree);
    const parentExisted = await pathExists(parent);
    await mkdir(parent, { recursive: true });
    try {
      await runCommand([
        "git",
        "-C",
        repository,
        "worktree",
        "add",
        "-b",
        branch,
        "--",
        worktree,
        base,
      ]);
      return worktree;
    } catch (error) {
      if (!parentExisted && (await readdir(parent)).length === 0) {
        await rm(parent, { recursive: true });
      }
      throw error;
    }
  });
}

export async function checkDispatch(
  rootInput: string,
  projectId: string,
  intentId: string,
): Promise<DispatchReadiness> {
  const root = resolve(rootInput);
  const validation = await doctorWorkspace(root);
  if (!validation.ok) {
    throw new PortfolioError(validation.errors.join("\n"));
  }

  const loaded = await loadWorkspace(root);
  const intent = loaded.intents.find((item) => item.id === intentId);
  const mapping = intent?.projects.find((item) => item.project === projectId);
  if (!intent || !mapping) {
    throw new PortfolioError(`project ${projectId} is not registered in intent ${intentId}`);
  }
  return assertDispatchable(root, loaded, mapping, projectId, intentId);
}

async function assertDispatchable(
  root: string,
  loaded: LoadedWorkspace,
  mapping: IntentProject,
  projectId: string,
  intentId: string,
): Promise<DispatchReadiness> {
  const discovery = await requireConfirmedDiscovery(root, loaded);
  requireLifecyclePhase(loaded, "dispatch");
  requireCurrentPlanAcceptance(loaded, discovery);
  const pendingQuestions = await listQuestionPackets(
    root,
    projectId,
    intentId,
    "pending",
  );
  if (pendingQuestions.length > 0) {
    throw new PortfolioError(
      `session ${intentId}:${projectId} has unanswered human questions: ${pendingQuestions
        .map((packet) => packet.id)
        .join(", ")}`,
    );
  }
  const current = loaded.state.sessions[sessionKey(intentId, projectId)];
  if (current?.status === "active" || current?.status === "waiting") {
    throw new PortfolioError(
      `session ${intentId}:${projectId} is already ${current.status}`,
    );
  }

  for (const dependencyProject of mapping.dependsOn) {
    const dependency = loaded.state.sessions[sessionKey(intentId, dependencyProject)];
    if (dependency?.status !== "completed") {
      throw new PortfolioError(
        `project ${projectId} is blocked by incomplete project ${dependencyProject}`,
      );
    }
  }
  for (const dependency of loaded.dependencies) {
    if (
      dependency.source.project !== projectId ||
      !dependency.blockingAt.includes("dispatch")
    ) {
      continue;
    }
    const targetProject = dependency.target.project;
    const target = loaded.state.sessions[sessionKey(intentId, targetProject)];
    if (target?.status !== "completed") {
      throw new PortfolioError(
        `project ${projectId} is blocked at dispatch by dependency ${dependency.id} on ${targetProject}`,
      );
    }
  }

  const worktree = resolveInside(root, mapping.worktree, workspacePaths(root).worktrees);
  if (!(await pathExists(worktree))) {
    throw new PortfolioError(`worktree does not exist: ${worktree}`);
  }
  const harness = await verifyHarness(root, "claude", projectId, intentId);
  if (!harness.ok) {
    const details = [
      ...harness.errors,
      ...harness.worktrees.flatMap((item) =>
        item.errors.map((error) => `${item.intent}:${item.project}: ${error}`),
      ),
    ];
    throw new PortfolioError(
      `AI-DLC harness verification failed: ${details.join("; ")}`,
    );
  }
  if (!(await hasAidlcHarness(worktree))) {
    throw new PortfolioError(`AI-DLC harness not found in worktree: ${worktree}`);
  }

  const aidlcSpace = mapping.aidlcSpace ?? "default";
  const projectMemory = await memorySnapshot(
    root,
    projectId,
    intentId,
    "project",
    aidlcSpace,
  );
  const teamMemory = await memorySnapshot(
    root,
    projectId,
    intentId,
    "team",
    aidlcSpace,
  );
  if (!projectMemory.inSync || !teamMemory.inSync) {
    throw new PortfolioError(
      `worktree memory is stale for AI-DLC space ${aidlcSpace}; inspect and refresh project and team memory before dispatch`,
    );
  }
  return {
    worktree,
    branch: mapping.branch,
    aidlcIntent: mapping.aidlcIntent,
    aidlcSpace,
    memory: {
      project: {
        revision: projectMemory.canonicalRevision,
        path: projectMemory.canonicalPath,
      },
      team: {
        revision: teamMemory.canonicalRevision,
        path: teamMemory.canonicalPath,
      },
    },
  };
}

export async function memorySnapshot(
  rootInput: string,
  projectId: string,
  intentId: string,
  destination: MemoryDestination,
  space = "default",
): Promise<MemorySnapshot> {
  assertId(space, "space");
  const root = resolve(rootInput);
  const loaded = await loadWorkspace(root);
  const { canonicalPath, relativeMemory, worktreePath } = resolveMemoryPaths(
    loaded,
    projectId,
    intentId,
    destination,
    space,
  );
  const canonicalContent = (await optionalRead(canonicalPath)) ?? "";
  const worktreeContent = (await optionalRead(worktreePath)) ?? "";
  const canonicalRevision = contentRevision(canonicalContent);
  const worktreeRevision = contentRevision(worktreeContent);
  const head = await readHeadMemory(
    dirnameFromRelative(worktreePath, relativeMemory),
    relativeMemory,
  );
  const headRevision = head ? contentRevision(head.content) : null;
  return {
    project: projectId,
    intent: intentId,
    space,
    destination,
    canonicalPath,
    worktreePath,
    canonicalRevision,
    worktreeRevision,
    inSync: canonicalRevision === worktreeRevision,
    headRevision,
    mergeClean: headRevision === null ? null : headRevision === worktreeRevision,
  };
}

export async function refreshWorktreeMemory(
  rootInput: string,
  projectId: string,
  intentId: string,
  destination: MemoryDestination,
  expectedWorktreeRevision: string,
  space = "default",
): Promise<MemorySnapshot> {
  if (!/^[a-f0-9]{64}$/.test(expectedWorktreeRevision)) {
    throw new PortfolioError("expected worktree revision must be a SHA-256 hash");
  }
  assertId(space, "space");
  const root = resolve(rootInput);
  return withLock(root, async () => {
    const loaded = await loadWorkspace(root);
    const { canonicalPath, relativeMemory, worktreePath } = resolveMemoryPaths(
      loaded,
      projectId,
      intentId,
      destination,
      space,
    );
    const canonicalContent = (await optionalRead(canonicalPath)) ?? "";
    const worktreeContent = (await optionalRead(worktreePath)) ?? "";
    const currentWorktreeRevision = contentRevision(worktreeContent);
    if (currentWorktreeRevision !== expectedWorktreeRevision) {
      throw new PortfolioError(
        `worktree memory changed after inspection: expected ${expectedWorktreeRevision}, current ${currentWorktreeRevision}`,
      );
    }
    await atomicWrite(worktreePath, canonicalContent);
    const canonicalRevision = contentRevision(canonicalContent);
    const head = await readHeadMemory(
      dirnameFromRelative(worktreePath, relativeMemory),
      relativeMemory,
    );
    const headRevision = head ? contentRevision(head.content) : null;
    return {
      project: projectId,
      intent: intentId,
      space,
      destination,
      canonicalPath,
      worktreePath,
      canonicalRevision,
      worktreeRevision: canonicalRevision,
      inSync: true,
      headRevision,
      mergeClean: headRevision === null ? null : headRevision === canonicalRevision,
    };
  });
}

export async function cleanWorktreeMemory(
  rootInput: string,
  projectId: string,
  intentId: string,
  destination: MemoryDestination,
  expectedWorktreeRevision: string,
  space = "default",
): Promise<MemorySnapshot> {
  if (!/^[a-f0-9]{64}$/.test(expectedWorktreeRevision)) {
    throw new PortfolioError("expected worktree revision must be a SHA-256 hash");
  }
  assertId(space, "space");
  const root = resolve(rootInput);
  return withLock(root, async () => {
    const loaded = await loadWorkspace(root);
    const { canonicalPath, relativeMemory, worktreePath } = resolveMemoryPaths(
      loaded,
      projectId,
      intentId,
      destination,
      space,
    );
    const worktreeContent = (await optionalRead(worktreePath)) ?? "";
    const currentWorktreeRevision = contentRevision(worktreeContent);
    if (currentWorktreeRevision !== expectedWorktreeRevision) {
      throw new PortfolioError(
        `worktree memory changed after inspection: expected ${expectedWorktreeRevision}, current ${currentWorktreeRevision}`,
      );
    }
    const worktree = dirnameFromRelative(worktreePath, relativeMemory);
    const head = await readHeadMemory(worktree, relativeMemory);
    if (!head) {
      throw new PortfolioError(`worktree is not a Git checkout: ${worktree}`);
    }
    if (head.exists) {
      await atomicWrite(worktreePath, head.content);
    } else {
      await rm(worktreePath, { force: true });
    }
    const canonicalContent = (await optionalRead(canonicalPath)) ?? "";
    const cleanedContent = head.exists ? head.content : "";
    return {
      project: projectId,
      intent: intentId,
      space,
      destination,
      canonicalPath,
      worktreePath,
      canonicalRevision: contentRevision(canonicalContent),
      worktreeRevision: contentRevision(cleanedContent),
      inSync: canonicalContent === cleanedContent,
      headRevision: contentRevision(cleanedContent),
      mergeClean: true,
    };
  });
}

export async function registerLearningProposal(
  rootInput: string,
  sourceFile: string,
): Promise<string> {
  const root = resolve(rootInput);
  const input = await readYaml<LearningProposal>(resolve(sourceFile));
  await validateLearningProposal(input);
  if (input.status && input.status !== "pending") {
    throw new PortfolioError("new learning proposal status must be pending");
  }

  return withLock(root, async () => {
    const loaded = await loadWorkspace(root);
    resolveMemoryPaths(
      loaded,
      input.project,
      input.intent,
      input.destination,
      input.space,
    );
    const target = learningPath(root, input.id);
    if (await pathExists(target)) {
      throw new PortfolioError(`learning proposal already exists: ${input.id}`);
    }
    const now = new Date().toISOString();
    const proposal: LearningProposal = {
      ...input,
      status: "pending",
      createdAt: now,
      updatedAt: now,
    };
    await atomicWrite(target, stringify(proposal));
    return target;
  });
}

export async function listLearningProposals(
  rootInput: string,
  projectId?: string,
  status?: LearningStatus,
): Promise<LearningProposal[]> {
  const proposals = await readLearningProposals(resolve(rootInput));
  return proposals.filter(
    (proposal) =>
      (!projectId || proposal.project === projectId) &&
      (!status || proposal.status === status),
  );
}

export async function reconcileLearningProposal(
  rootInput: string,
  id: string,
  note: string,
): Promise<{ id: string; baseRevision: string }> {
  if (!note.trim()) {
    throw new PortfolioError("reconciliation note must not be empty");
  }
  const root = resolve(rootInput);
  return withLock(root, async () => {
    const proposal = await readLearningProposal(root, id);
    requirePending(proposal);
    const loaded = await loadWorkspace(root);
    const { canonicalPath } = resolveMemoryPaths(
      loaded,
      proposal.project,
      proposal.intent,
      proposal.destination,
      proposal.space,
    );
    const revision = contentRevision((await optionalRead(canonicalPath)) ?? "");
    const now = new Date().toISOString();
    proposal.reconciliations = [
      ...(proposal.reconciliations ?? []),
      {
        note: note.trim(),
        reconciledAt: now,
        previousRevision: proposal.baseRevision,
      },
    ];
    proposal.baseRevision = revision;
    proposal.updatedAt = now;
    await atomicWrite(learningPath(root, id), stringify(proposal));
    return { id, baseRevision: revision };
  });
}

export async function approveLearningProposal(
  rootInput: string,
  id: string,
): Promise<{ id: string; outcome: "applied" | "already-present"; path: string }> {
  const root = resolve(rootInput);
  return withLock(root, async () => {
    const proposal = await readLearningProposal(root, id);
    requirePending(proposal);
    const loaded = await loadWorkspace(root);
    const { canonicalPath } = resolveMemoryPaths(
      loaded,
      proposal.project,
      proposal.intent,
      proposal.destination,
      proposal.space,
    );
    const content = (await optionalRead(canonicalPath)) ?? "";
    const marker = `<!-- portfolio-learning:${proposal.id} -->`;
    const alreadyPresent =
      content.includes(marker) || containsEquivalentRule(content, proposal.rule);
    const currentRevision = contentRevision(content);
    if (!alreadyPresent && currentRevision !== proposal.baseRevision) {
      throw new PortfolioError(
        `learning proposal ${id} is stale: base ${proposal.baseRevision}, current ${currentRevision}; reconcile it before approval`,
      );
    }

    if (!alreadyPresent) {
      await atomicWrite(
        canonicalPath,
        appendRule(content, proposal.heading, proposal.rule, marker),
      );
    }
    const now = new Date().toISOString();
    proposal.status = "applied";
    proposal.appliedAt = now;
    proposal.updatedAt = now;
    await atomicWrite(learningPath(root, id), stringify(proposal));
    return {
      id,
      outcome: alreadyPresent ? "already-present" : "applied",
      path: canonicalPath,
    };
  });
}

export async function rejectLearningProposal(
  rootInput: string,
  id: string,
  reason: string,
): Promise<void> {
  if (!reason.trim()) {
    throw new PortfolioError("rejection reason must not be empty");
  }
  const root = resolve(rootInput);
  await withLock(root, async () => {
    const proposal = await readLearningProposal(root, id);
    requirePending(proposal);
    const now = new Date().toISOString();
    proposal.status = "rejected";
    proposal.rejectedAt = now;
    proposal.rejectionReason = reason.trim();
    proposal.updatedAt = now;
    await atomicWrite(learningPath(root, id), stringify(proposal));
  });
}

async function loadWorkspace(root: string): Promise<LoadedWorkspace> {
  const paths = workspacePaths(root);
  const portfolio = await readAndValidateYaml<PortfolioDocument>(
    paths.portfolioFile,
    "portfolio",
  );
  const stateDocument = await readJson<PortfolioState | LegacyPortfolioState>(
    paths.stateFile,
  );
  if (stateDocument.schemaVersion === 1) {
    throw new PortfolioError(
      "state.json schemaVersion 1 requires migration; run lifecycle migrate",
    );
  }
  const state = stateDocument;
  const projects = await readDocuments<ProjectDocument>(paths.projects, "project");
  const dependencies = await readDocuments<DependencyDocument>(
    paths.dependencies,
    "dependency",
  );
  const intents = await readDocuments<IntentDocument>(paths.intents, "intent");
  return { root, portfolio, state, projects, dependencies, intents };
}

async function readDocuments<T>(directory: string, kind: DocumentKind): Promise<T[]> {
  if (!(await pathExists(directory))) {
    return [];
  }
  const names = (await readdir(directory))
    .filter((name) => name.endsWith(".yaml") || name.endsWith(".yml"))
    .sort();
  return Promise.all(names.map((name) => readAndValidateYaml<T>(join(directory, name), kind)));
}

async function readAndValidateYaml<T>(
  file: string,
  kind: DocumentKind | "portfolio",
): Promise<T> {
  const document = await readYaml<T>(file);
  const validate = await schemaValidator(kind);
  if (!validate(document)) {
    throw new PortfolioError(`${file}: ${formatSchemaErrors(kind, validate.errors)}`);
  }
  return document;
}

async function schemaValidator(
  kind: DocumentKind | "portfolio",
): Promise<ValidateFunction> {
  const schemaPath = new URL(`../assets/schemas/${SCHEMA_FILES[kind]}`, import.meta.url);
  const schema = JSON.parse(await readFile(schemaPath, "utf8"));
  const ajv = new Ajv2020({ allErrors: true, strict: true });
  return ajv.compile(schema);
}

async function learningSchemaValidator(): Promise<ValidateFunction> {
  const schemaPath = new URL(
    "../assets/schemas/learning-proposal.schema.json",
    import.meta.url,
  );
  const schema = JSON.parse(await readFile(schemaPath, "utf8"));
  const ajv = new Ajv2020({ allErrors: true, strict: true });
  return ajv.compile(schema);
}

async function discoverySchemaValidator(): Promise<ValidateFunction> {
  const schemaPath = new URL(
    "../assets/schemas/discovery-decision.schema.json",
    import.meta.url,
  );
  const schema = JSON.parse(await readFile(schemaPath, "utf8"));
  const ajv = new Ajv2020({ allErrors: true, strict: true });
  return ajv.compile(schema);
}

function formatSchemaErrors(
  kind: string,
  errors: ErrorObject[] | null | undefined,
): string {
  const details = (errors ?? [])
    .map((error) => `${error.instancePath || "/"} ${error.message}`)
    .join("; ");
  return `${kind} schema validation failed: ${details || "unknown error"}`;
}

function validateState(loaded: LoadedWorkspace, errors: string[]): void {
  if (loaded.state.schemaVersion !== 2) {
    errors.push(
      "state.json schemaVersion must be 2; run lifecycle migrate for an existing workspace",
    );
  }
  if (loaded.state.portfolioId !== loaded.portfolio.id) {
    errors.push("state.json portfolioId does not match portfolio.yaml");
  }
  if (
    loaded.state.activeIntent &&
    !loaded.intents.some((intent) => intent.id === loaded.state.activeIntent)
  ) {
    errors.push(`state.json activeIntent is unknown: ${loaded.state.activeIntent}`);
  }
  if (!loaded.state.lifecycle) {
    errors.push("state.json lifecycle is missing");
  } else {
    if (!PORTFOLIO_PHASES.includes(loaded.state.lifecycle.phase)) {
      errors.push(
        `state.json lifecycle phase is invalid: ${loaded.state.lifecycle.phase}`,
      );
    }
    if (!["running", "completed"].includes(loaded.state.lifecycle.status)) {
      errors.push(
        `state.json lifecycle status is invalid: ${loaded.state.lifecycle.status}`,
      );
    }
    if (
      loaded.state.lifecycle.status === "completed" &&
      (!loaded.state.lifecycle.completedAt ||
        loaded.state.lifecycle.phase !== "learn")
    ) {
      errors.push(
        "completed lifecycle must remain at learn with completedAt recorded",
      );
    }
    const history = loaded.state.lifecycle.history;
    if (!Array.isArray(history) || history.length === 0) {
      errors.push("state.json lifecycle history must not be empty");
    } else {
      if (history[0]?.from !== null) {
        errors.push("state.json lifecycle history must begin from null");
      }
      if (history.at(-1)?.to !== loaded.state.lifecycle.phase) {
        errors.push(
          "state.json lifecycle history does not end at the current phase",
        );
      }
      for (let index = 1; index < history.length; index += 1) {
        const previous = history[index - 1]!;
        const current = history[index]!;
        const selfEvent =
          (current.from === "learn" && current.to === "learn") ||
          (current.from === "dispatch" && current.to === "dispatch");
        if (!selfEvent && current.from !== previous.to) {
          errors.push(
            `state.json lifecycle history is discontinuous at entry ${index}`,
          );
        }
        if (
          !selfEvent &&
          current.from &&
          NEXT_PHASE[current.from] !== current.to
        ) {
          errors.push(
            `state.json lifecycle history has invalid transition ${current.from} -> ${current.to}`,
          );
        }
      }
    }
  }

  const expectedSessions = new Set(
    loaded.intents.flatMap((intent) =>
      intent.projects.map((mapping) => sessionKey(intent.id, mapping.project)),
    ),
  );
  for (const [key, session] of Object.entries(loaded.state.sessions)) {
    if (!expectedSessions.has(key)) {
      errors.push(`state.json contains an unknown session: ${key}`);
    }
    if (!SESSION_STATUSES.includes(session.status)) {
      errors.push(`state.json session ${key} has invalid status: ${session.status}`);
    }
  }
}

function validateProjectPaths(loaded: LoadedWorkspace, errors: string[]): void {
  const seen = new Set<string>();
  for (const project of loaded.projects) {
    if (seen.has(project.id)) {
      errors.push(`duplicate project id: ${project.id}`);
    }
    seen.add(project.id);
    try {
      resolveInside(
        loaded.root,
        project.repository,
        workspacePaths(loaded.root).repositories,
      );
    } catch (error) {
      errors.push(errorMessage(error));
    }
  }
}

function validateDependencies(
  loaded: LoadedWorkspace,
  projects: Map<string, ProjectDocument>,
  errors: string[],
): void {
  for (const dependency of loaded.dependencies) {
    validateEndpoint(dependency.id, "source", dependency.source, projects, errors);
    validateEndpoint(dependency.id, "target", dependency.target, projects, errors);
  }
}

function validateEndpoint(
  dependencyId: string,
  label: string,
  endpoint: { project: string; component?: string },
  projects: Map<string, ProjectDocument>,
  errors: string[],
): void {
  const project = projects.get(endpoint.project);
  if (!project) {
    errors.push(`dependency ${dependencyId} ${label} project is unknown: ${endpoint.project}`);
    return;
  }
  if (
    endpoint.component &&
    !project.components.some((component) => component.id === endpoint.component)
  ) {
    errors.push(
      `dependency ${dependencyId} ${label} component is unknown: ${endpoint.component}`,
    );
  }
}

function validateIntents(
  loaded: LoadedWorkspace,
  projects: Map<string, ProjectDocument>,
  errors: string[],
): void {
  const activeWorktrees = new Map<string, string>();
  for (const intent of loaded.intents) {
    const projectIds = new Set(intent.projects.map((mapping) => mapping.project));
    for (const mapping of intent.projects) {
      if (!projects.has(mapping.project)) {
        errors.push(`intent ${intent.id} references unknown project ${mapping.project}`);
      }
      for (const dependency of mapping.dependsOn) {
        if (!projectIds.has(dependency)) {
          errors.push(
            `intent ${intent.id} project ${mapping.project} depends on unknown project ${dependency}`,
          );
        }
      }
      try {
        const worktree = resolveInside(
          loaded.root,
          mapping.worktree,
          workspacePaths(loaded.root).worktrees,
        );
        if (["proposed", "active", "blocked"].includes(intent.status)) {
          const owner = activeWorktrees.get(worktree);
          if (owner) {
            errors.push(`worktree ${worktree} is shared by ${owner} and ${intent.id}`);
          }
          activeWorktrees.set(worktree, intent.id);
        }
      } catch (error) {
        errors.push(errorMessage(error));
      }
    }
  }
}

async function ensureIntentSessions(root: string, intent: IntentDocument): Promise<void> {
  const paths = workspacePaths(root);
  const state = await readJson<PortfolioState>(paths.stateFile);
  const now = new Date().toISOString();
  const expectedKeys = new Set(
    intent.projects.map((mapping) => sessionKey(intent.id, mapping.project)),
  );
  for (const key of Object.keys(state.sessions)) {
    if (key.startsWith(`${intent.id}:`) && !expectedKeys.has(key)) {
      delete state.sessions[key];
    }
  }
  for (const mapping of intent.projects) {
    const key = sessionKey(intent.id, mapping.project);
    state.sessions[key] ??= {
      status: "pending",
      terminalId: null,
      updatedAt: now,
    };
  }
  state.updatedAt = now;
  await atomicWrite(paths.stateFile, `${JSON.stringify(state, null, 2)}\n`);
}

async function ensureDirectories(root: string): Promise<void> {
  await Promise.all(
    REQUIRED_DIRECTORIES.map((directory) =>
      mkdir(join(root, directory), { recursive: true }),
    ),
  );
}

async function hasAidlcHarness(worktree: string): Promise<boolean> {
  const candidates = [
    ".claude/skills/aidlc/SKILL.md",
    ".kiro/skills/aidlc/SKILL.md",
    ".codex/skills/aidlc/SKILL.md",
  ];
  for (const candidate of candidates) {
    if (await pathExists(join(worktree, candidate))) {
      return true;
    }
  }
  return false;
}

function resolveMemoryPaths(
  loaded: LoadedWorkspace,
  projectId: string,
  intentId: string,
  destination: MemoryDestination,
  space: string,
): { canonicalPath: string; worktreePath: string; relativeMemory: string } {
  const project = loaded.projects.find((item) => item.id === projectId);
  const intent = loaded.intents.find((item) => item.id === intentId);
  const mapping = intent?.projects.find((item) => item.project === projectId);
  if (!project || !mapping) {
    throw new PortfolioError(`project ${projectId} is not registered in intent ${intentId}`);
  }
  const assignedSpace = mapping.aidlcSpace ?? "default";
  if (space !== assignedSpace) {
    throw new PortfolioError(
      `intent ${intentId} project ${projectId} uses AI-DLC space ${assignedSpace}, not ${space}`,
    );
  }
  const relativeMemory = join("aidlc", "spaces", space, "memory", `${destination}.md`);
  const repository = resolveInside(
    loaded.root,
    project.repository,
    workspacePaths(loaded.root).repositories,
  );
  const worktree = resolveInside(
    loaded.root,
    mapping.worktree,
    workspacePaths(loaded.root).worktrees,
  );
  return {
    canonicalPath: join(repository, relativeMemory),
    worktreePath: join(worktree, relativeMemory),
    relativeMemory,
  };
}

async function validateLearningProposal(proposal: LearningProposal): Promise<void> {
  const validate = await learningSchemaValidator();
  if (!validate(proposal)) {
    throw new PortfolioError(
      formatSchemaErrors("learning proposal", validate.errors),
    );
  }
}

async function validateDiscoveryDecision(
  decision: DiscoveryDecision,
): Promise<void> {
  const validate = await discoverySchemaValidator();
  if (!validate(decision)) {
    throw new PortfolioError(
      formatSchemaErrors("discovery decision", validate.errors),
    );
  }
}

function validateDiscoveryAcceptance(
  decision: DiscoveryDecision,
  loaded: LoadedWorkspace,
): void {
  const facts: DiscoveryFactName[] = [
    "organization",
    "businessOutcomes",
    "businessCapabilities",
    "dependencies",
  ];
  const unknowns = facts.filter(
    (fact) => decision[fact].disposition === "unknown",
  );
  const deferrals = facts.filter(
    (fact) => decision[fact].disposition === "deferred",
  );
  if (!sameSet(decision.acceptance.unknowns, unknowns)) {
    throw new PortfolioError(
      `acceptance.unknowns must explicitly list: ${unknowns.join(", ") || "(none)"}`,
    );
  }
  if (!sameSet(decision.acceptance.deferrals, deferrals)) {
    throw new PortfolioError(
      `acceptance.deferrals must explicitly list: ${deferrals.join(", ") || "(none)"}`,
    );
  }
  if (
    decision.organization.disposition === "confirmed" &&
    (!decision.organization.value.trim() ||
      decision.organization.value.trim().toLocaleLowerCase() === "unknown")
  ) {
    throw new PortfolioError(
      "confirmed organization requires a known organization name",
    );
  }
  for (const fact of ["businessOutcomes", "businessCapabilities"] as const) {
    if (
      decision[fact].disposition === "confirmed" &&
      decision[fact].values.length === 0
    ) {
      throw new PortfolioError(`confirmed ${fact} requires at least one value`);
    }
  }
  const dependencyIds = loaded.dependencies.map((item) => item.id);
  if (!sameSet(decision.dependencies.ids, dependencyIds)) {
    throw new PortfolioError(
      "discovery dependencies must list every registered dependency exactly once",
    );
  }
}

async function requireConfirmedDiscovery(
  root: string,
  loaded: LoadedWorkspace,
): Promise<DiscoveryDecision> {
  const file = workspacePaths(root).discoveryFile;
  if (!(await pathExists(file))) {
    throw new PortfolioError(
      "portfolio discovery is not confirmed; run discovery confirm before dispatch",
    );
  }
  const decision = await readYaml<DiscoveryDecision>(file);
  await validateDiscoveryDecision(decision);
  validateDiscoveryAcceptance(decision, loaded);
  const currentRevision = catalogRevisionFor(loaded);
  if (decision.catalogRevision !== currentRevision) {
    throw new PortfolioError(
      "portfolio discovery confirmation is stale; review and confirm the current catalog before dispatch",
    );
  }
  return decision;
}

async function lifecycleSummary(loaded: LoadedWorkspace) {
  const lifecycle = loaded.state.lifecycle;
  const nextPhase = NEXT_PHASE[lifecycle.phase];
  let blockers =
    lifecycle.status === "completed"
      ? []
      : nextPhase
        ? await transitionBlockers(loaded.root, loaded, nextPhase)
        : await integrationBlockers(loaded.root, loaded);
  let actions =
    lifecycle.status === "completed"
      ? []
      : nextPhase
        ? [
            `lifecycle advance --to ${nextPhase}${
              nextPhase === "dispatch" ? " --accepted-by <human>" : ""
            }`,
          ]
        : ["lifecycle complete [--actor <name>]"];
  if (lifecycle.status === "running" && lifecycle.phase === "dispatch") {
    const acceptanceBlockers = await planAcceptanceBlockers(loaded.root, loaded);
    if (acceptanceBlockers.length > 0) {
      blockers = [...acceptanceBlockers, ...blockers];
      actions = [
        "lifecycle advance --to dispatch --accepted-by <human>",
      ];
    }
  }
  return {
    status: lifecycle.status,
    phase: lifecycle.phase,
    nextPhase,
    blockers,
    actions,
    planAcceptance: lifecycle.planAcceptance,
    completedAt: lifecycle.completedAt,
    history: lifecycle.history,
  };
}

async function transitionBlockers(
  root: string,
  loaded: LoadedWorkspace,
  target: PortfolioPhase,
  actor?: string,
): Promise<string[]> {
  const blockers: string[] = [];
  if (target === "discover") {
    if (loaded.projects.length === 0) {
      blockers.push("register at least one project");
    }
    if (loaded.intents.length === 0) {
      blockers.push("register at least one intent");
    }
    return blockers;
  }

  if (target === "confirm" || target === "plan" || target === "dispatch") {
    try {
      await requireConfirmedDiscovery(root, loaded);
    } catch (error) {
      blockers.push(errorMessage(error));
    }
  }

  if (target === "dispatch") {
    if (!actor?.trim()) {
      blockers.push("record explicit plan acceptance with --accepted-by <human>");
    }
    const harness = await verifyHarness(root, "claude");
    if (!harness.ok) {
      blockers.push(
        ...harness.errors.map((error) => `harness: ${error}`),
        ...harness.worktrees.flatMap((worktree) =>
          worktree.errors.map(
            (error) => `harness ${worktree.intent}:${worktree.project}: ${error}`,
          ),
        ),
      );
    }
  }

  if (target === "integrate") {
    blockers.push(...(await planAcceptanceBlockers(root, loaded)));
    blockers.push(...sessionCompletionBlockers(loaded));
  }
  if (target === "learn") {
    blockers.push(...(await integrationBlockers(root, loaded)));
  }
  return blockers;
}

function sessionCompletionBlockers(loaded: LoadedWorkspace): string[] {
  const sessions = Object.entries(loaded.state.sessions);
  if (sessions.length === 0) {
    return ["portfolio has no child sessions"];
  }
  return sessions
    .filter(([, session]) => session.status !== "completed")
    .map(([key, session]) => `child session ${key} is ${session.status}`);
}

async function integrationBlockers(
  root: string,
  loaded: LoadedWorkspace,
): Promise<string[]> {
  const blockers = sessionCompletionBlockers(loaded);
  const questions = await listQuestionPackets(root, undefined, undefined, "pending");
  blockers.push(
    ...questions.map((question) => `human question ${question.id} is unanswered`),
  );
  const learnings = await readLearningProposals(root);
  blockers.push(
    ...learnings
      .filter((learning) => learning.status === "pending")
      .map((learning) => `shared-memory proposal ${learning.id} is pending`),
  );
  blockers.push(
    ...loaded.dependencies
      .filter(
        (dependency) =>
          dependency.type === "contract" &&
          !["verified", "retired"].includes(dependency.status),
      )
      .map(
        (dependency) =>
          `contract dependency ${dependency.id} is ${dependency.status}`,
      ),
  );
  try {
    const convergence = await checkConvergence(root);
    blockers.push(
      ...convergence.risks
        .filter(
          (risk) =>
            risk.effectiveDisposition !== "satisfied" &&
            !(risk.effectiveDisposition === "deferred" && risk.decision),
        )
        .map((risk) => `convergence ${risk.id}: ${risk.message}`),
    );
  } catch (error) {
    blockers.push(`convergence check failed: ${errorMessage(error)}`);
  }
  return blockers;
}

async function inferMigrationPhase(
  root: string,
  state: LegacyPortfolioState,
): Promise<PortfolioPhase> {
  const sessions = Object.values(state.sessions);
  if (
    sessions.some((session) =>
      ["active", "waiting", "blocked", "failed"].includes(session.status),
    )
  ) {
    return "dispatch";
  }
  if (
    sessions.length > 0 &&
    sessions.every((session) => session.status === "completed")
  ) {
    return "integrate";
  }
  if (await pathExists(workspacePaths(root).discoveryFile)) {
    return "confirm";
  }
  const paths = workspacePaths(root);
  const [projects, intents] = await Promise.all([
    yamlDocumentCount(paths.projects),
    yamlDocumentCount(paths.intents),
  ]);
  return projects > 0 || intents > 0 ? "discover" : "bootstrap";
}

async function yamlDocumentCount(directory: string): Promise<number> {
  if (!(await pathExists(directory))) return 0;
  return (await readdir(directory)).filter(
    (name) => name.endsWith(".yaml") || name.endsWith(".yml"),
  ).length;
}

function requireLifecyclePhase(
  loaded: LoadedWorkspace,
  expected: PortfolioPhase,
): void {
  const lifecycle = loaded.state.lifecycle;
  if (lifecycle.status !== "running" || lifecycle.phase !== expected) {
    throw new PortfolioError(
      `portfolio lifecycle must be running in ${expected}, currently ${lifecycle.status}:${lifecycle.phase}`,
    );
  }
}

async function planAcceptanceBlockers(
  root: string,
  loaded: LoadedWorkspace,
): Promise<string[]> {
  try {
    const discovery = await requireConfirmedDiscovery(root, loaded);
    if (!loaded.state.lifecycle.planAcceptance) {
      return [
        "current plan is not accepted; run lifecycle advance --to dispatch --accepted-by <human>",
      ];
    }
    if (
      loaded.state.lifecycle.planAcceptance.catalogRevision !==
      discovery.catalogRevision
    ) {
      return [
        "plan acceptance is stale; run lifecycle advance --to dispatch --accepted-by <human>",
      ];
    }
    return [];
  } catch (error) {
    return [errorMessage(error)];
  }
}

function requireCurrentPlanAcceptance(
  loaded: LoadedWorkspace,
  discovery: DiscoveryDecision,
): void {
  const acceptance = loaded.state.lifecycle.planAcceptance;
  if (!acceptance) {
    throw new PortfolioError(
      "current plan is not accepted; run lifecycle advance --to dispatch --accepted-by <human>",
    );
  }
  if (acceptance.catalogRevision !== discovery.catalogRevision) {
    throw new PortfolioError(
      "plan acceptance is stale; run lifecycle advance --to dispatch --accepted-by <human>",
    );
  }
}

function requirePortfolioPhase(value: string): PortfolioPhase {
  if (!PORTFOLIO_PHASES.includes(value as PortfolioPhase)) {
    throw new PortfolioError(`invalid portfolio lifecycle phase: ${value}`);
  }
  return value as PortfolioPhase;
}

async function writePortfolioState(
  root: string,
  state: PortfolioState,
): Promise<void> {
  await atomicWrite(
    workspacePaths(root).stateFile,
    `${JSON.stringify(state, null, 2)}\n`,
  );
}

function catalogRevisionFor(loaded: LoadedWorkspace): string {
  return contentRevision(
    JSON.stringify({
      portfolio: loaded.portfolio,
      projects: loaded.projects,
      dependencies: loaded.dependencies,
      intents: loaded.intents,
    }),
  );
}

function sameSet(left: string[], right: string[]): boolean {
  return (
    left.length === new Set(left).size &&
    right.length === new Set(right).size &&
    left.length === right.length &&
    left.every((value) => right.includes(value))
  );
}

function parseQuestionDocument(content: string): {
  answers: string[];
  skeleton: string;
} {
  const marker = /^\[Answer\]:[ \t]*(.*)$/gm;
  const answers = [...content.matchAll(marker)].map((match) => match[1] ?? "");
  if (answers.length === 0) {
    throw new PortfolioError(
      "question document must contain at least one [Answer]: marker",
    );
  }
  return {
    answers,
    skeleton: content.replace(marker, "[Answer]:"),
  };
}

function questionDocumentPath(root: string, id: string): string {
  assertId(id, "question packet id");
  return join(workspacePaths(root).questions, `${id}.md`);
}

function questionMetadataPath(root: string, id: string): string {
  assertId(id, "question packet id");
  return join(workspacePaths(root).questions, `${id}.json`);
}

async function readQuestionPacket(
  root: string,
  id: string,
): Promise<QuestionPacket> {
  return readJson<QuestionPacket>(questionMetadataPath(root, id));
}

function requireIntentProject(
  loaded: LoadedWorkspace,
  intentId: string,
  projectId: string,
): void {
  const intent = loaded.intents.find((item) => item.id === intentId);
  if (!intent?.projects.some((mapping) => mapping.project === projectId)) {
    throw new PortfolioError(
      `project ${projectId} is not registered in intent ${intentId}`,
    );
  }
}

function requireSessionStatus(
  loaded: LoadedWorkspace,
  intentId: string,
  projectId: string,
  expected: SessionStatus,
): void {
  const actual = loaded.state.sessions[sessionKey(intentId, projectId)]?.status;
  if (actual !== expected) {
    throw new PortfolioError(
      `session ${intentId}:${projectId} must be ${expected}, not ${actual ?? "missing"}`,
    );
  }
}

async function setSessionStatus(
  loaded: LoadedWorkspace,
  intentId: string,
  projectId: string,
  status: SessionStatus,
): Promise<void> {
  const key = sessionKey(intentId, projectId);
  const previous = loaded.state.sessions[key];
  loaded.state.sessions[key] = {
    status,
    terminalId: previous?.terminalId ?? null,
    updatedAt: new Date().toISOString(),
  };
  loaded.state.activeIntent = activeIntentFrom(loaded.state.sessions);
  loaded.state.updatedAt = new Date().toISOString();
  await atomicWrite(
    workspacePaths(loaded.root).stateFile,
    `${JSON.stringify(loaded.state, null, 2)}\n`,
  );
}

function activeIntentFrom(
  sessions: Record<string, SessionRecord>,
): string | null {
  const active = Object.entries(sessions)
    .filter(([, session]) =>
      session.status === "active" || session.status === "waiting",
    )
    .sort((left, right) =>
      right[1].updatedAt.localeCompare(left[1].updatedAt),
    );
  return active[0]?.[0].split(":")[0] ?? null;
}

async function readLearningProposals(root: string): Promise<LearningProposal[]> {
  const directory = workspacePaths(root).learnings;
  if (!(await pathExists(directory))) return [];
  const names = (await readdir(directory))
    .filter((name) => name.endsWith(".yaml") || name.endsWith(".yml"))
    .sort();
  return Promise.all(
    names.map(async (name) => {
      const proposal = await readYaml<LearningProposal>(join(directory, name));
      await validateLearningProposal(proposal);
      return proposal;
    }),
  );
}

async function readLearningProposal(
  root: string,
  id: string,
): Promise<LearningProposal> {
  assertId(id, "learning proposal id");
  const proposal = await readYaml<LearningProposal>(learningPath(root, id));
  await validateLearningProposal(proposal);
  return proposal;
}

function learningPath(root: string, id: string): string {
  assertId(id, "learning proposal id");
  return join(workspacePaths(root).learnings, `${id}.yaml`);
}

function requirePending(proposal: LearningProposal): void {
  if (proposal.status !== "pending") {
    throw new PortfolioError(
      `learning proposal ${proposal.id} is ${proposal.status ?? "invalid"}`,
    );
  }
}

function contentRevision(content: string): string {
  return createHash("sha256").update(content).digest("hex");
}

function containsEquivalentRule(content: string, rule: string): boolean {
  const expected = normalizeRule(rule);
  return content
    .split(/\r?\n/)
    .filter((line) => line.trimStart().startsWith("- "))
    .some((line) => normalizeRule(line) === expected);
}

function normalizeRule(rule: string): string {
  return rule
    .replace(/^\s*-\s*/, "")
    .replace(/\s*<!--[\s\S]*?-->\s*$/, "")
    .replace(/\s+\(learned \d{4}-\d{2}-\d{2}\)\s*$/, "")
    .replace(/\s+/g, " ")
    .trim()
    .toLocaleLowerCase();
}

function appendRule(
  content: string,
  heading: string,
  rule: string,
  marker: string,
): string {
  const normalized = content.replace(/\s+$/, "");
  const section = `## ${heading}`;
  const entry = `- ${rule.trim()} ${marker}`;
  if (!normalized) {
    return `${section}\n\n${entry}\n`;
  }

  const lines = normalized.split(/\r?\n/);
  const sectionIndex = lines.findIndex((line) => line.trim() === section);
  if (sectionIndex === -1) {
    return `${normalized}\n\n${section}\n\n${entry}\n`;
  }
  const nextSection = lines.findIndex(
    (line, index) => index > sectionIndex && line.startsWith("## "),
  );
  const insertionIndex = nextSection === -1 ? lines.length : nextSection;
  lines.splice(insertionIndex, 0, entry, "");
  return `${lines.join("\n").replace(/\n{3,}/g, "\n\n").trimEnd()}\n`;
}

function resolveInside(root: string, value: string, boundary: string): string {
  const resolved = resolve(root, value);
  const boundaryResolved = resolve(boundary);
  const child = relative(boundaryResolved, resolved);
  if (child === "" || (!child.startsWith(`..${sep}`) && child !== "..")) {
    return resolved;
  }
  throw new PortfolioError(`${value} must resolve inside ${boundaryResolved}`);
}

async function readYaml<T>(file: string): Promise<T> {
  try {
    return parse(await readFile(file, "utf8")) as T;
  } catch (error) {
    throw new PortfolioError(`cannot read YAML ${file}: ${errorMessage(error)}`);
  }
}

async function readJson<T>(file: string): Promise<T> {
  try {
    return JSON.parse(await readFile(file, "utf8")) as T;
  } catch (error) {
    throw new PortfolioError(`cannot read JSON ${file}: ${errorMessage(error)}`);
  }
}

async function optionalRead(file: string): Promise<string | null> {
  return (await pathExists(file)) ? readFile(file, "utf8") : null;
}

async function restoreFile(file: string, content: string | null): Promise<void> {
  if (content === null) {
    await rm(file, { force: true });
    return;
  }
  await atomicWrite(file, content);
}

async function atomicWrite(file: string, content: string): Promise<void> {
  await mkdir(dirname(file), { recursive: true });
  const temporary = join(
    dirname(file),
    `.${basename(file)}.${process.pid}.${crypto.randomUUID()}.tmp`,
  );
  await writeFile(temporary, content, { encoding: "utf8", flag: "wx" });
  await rename(temporary, file);
}

async function withLock<T>(root: string, operation: () => Promise<T>): Promise<T> {
  const lock = join(workspacePaths(root).portfolio, ".portfolio.lock");
  const owner: PortfolioLockOwner = {
    token: crypto.randomUUID(),
    pid: process.pid,
    createdAt: new Date().toISOString(),
  };
  await acquirePortfolioLock(lock, owner);
  try {
    return await operation();
  } finally {
    await releasePortfolioLock(lock, owner.token);
  }
}

async function acquirePortfolioLock(
  lock: string,
  owner: PortfolioLockOwner,
): Promise<void> {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      await mkdir(lock);
    } catch (error) {
      if (
        !isFileSystemError(error, "EEXIST") ||
        attempt > 0 ||
        !(await reclaimAbandonedLock(lock))
      ) {
        throw new PortfolioError(
          `portfolio is locked by another operation: ${lock}`,
        );
      }
      continue;
    }

    try {
      await writeFile(
        join(lock, "owner.json"),
        `${JSON.stringify(owner, null, 2)}\n`,
        { flag: "wx" },
      );
      return;
    } catch (error) {
      await rm(lock, { recursive: true, force: true });
      throw error;
    }
  }
}

async function reclaimAbandonedLock(lock: string): Promise<boolean> {
  try {
    const owner = JSON.parse(
      await readFile(join(lock, "owner.json"), "utf8"),
    ) as Partial<PortfolioLockOwner>;
    if (
      typeof owner.pid === "number" &&
      Number.isInteger(owner.pid) &&
      owner.pid > 0 &&
      typeof owner.createdAt === "string"
    ) {
      if (processIsAlive(owner.pid)) return false;
      await rm(lock, { recursive: true, force: true });
      return true;
    }
  } catch (error) {
    if (isFileSystemError(error, "ENOENT")) {
      try {
        const metadata = await stat(lock);
        if (Date.now() - metadata.mtimeMs < LOCK_OWNER_GRACE_MS) return false;
      } catch (statError) {
        return isFileSystemError(statError, "ENOENT");
      }
    } else if (!(error instanceof SyntaxError)) {
      throw error;
    }
  }

  const metadata = await stat(lock);
  if (Date.now() - metadata.mtimeMs < LOCK_OWNER_GRACE_MS) return false;
  await rm(lock, { recursive: true, force: true });
  return true;
}

function processIsAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return !isFileSystemError(error, "ESRCH");
  }
}

async function releasePortfolioLock(lock: string, token: string): Promise<void> {
  try {
    const owner = JSON.parse(
      await readFile(join(lock, "owner.json"), "utf8"),
    ) as Partial<PortfolioLockOwner>;
    if (owner.token === token) {
      await rm(lock, { recursive: true, force: true });
    }
  } catch (error) {
    if (!isFileSystemError(error, "ENOENT")) throw error;
  }
}

async function runCommand(command: string[]): Promise<void> {
  const process = Bun.spawn(command, { stdout: "pipe", stderr: "pipe" });
  const [exitCode, stdout, stderr] = await Promise.all([
    process.exited,
    new Response(process.stdout).text(),
    new Response(process.stderr).text(),
  ]);
  if (exitCode !== 0) {
    throw new PortfolioError(
      `${command.join(" ")} failed (${exitCode}): ${stderr.trim() || stdout.trim()}`,
    );
  }
}

async function readHeadMemory(
  worktree: string,
  relativeMemory: string,
): Promise<{ exists: boolean; content: string } | null> {
  if (!(await pathExists(join(worktree, ".git")))) return null;
  const gitPath = relativeMemory.split(sep).join("/");
  await runGitForMemory(worktree, ["rev-parse", "--verify", "HEAD^{commit}"]);
  const treeEntry = await runGitForMemory(worktree, [
    "ls-tree",
    "--name-only",
    "HEAD",
    "--",
    gitPath,
  ]);
  if (!treeEntry.trim()) return { exists: false, content: "" };
  const content = await runGitForMemory(worktree, ["show", `HEAD:${gitPath}`]);
  return { exists: true, content };
}

async function runGitForMemory(
  worktree: string,
  args: string[],
): Promise<string> {
  const child = Bun.spawn(["git", "-C", worktree, ...args], {
    stdout: "pipe",
    stderr: "pipe",
  });
  const [exitCode, stdout, stderr] = await Promise.all([
    child.exited,
    new Response(child.stdout).text(),
    new Response(child.stderr).text(),
  ]);
  if (exitCode !== 0) {
    throw new PortfolioError(
      `cannot inspect worktree memory with git ${args.join(" ")}: ${
        stderr.trim() || stdout.trim()
      }`,
    );
  }
  return stdout;
}

function dirnameFromRelative(file: string, relativePath: string): string {
  let root = file;
  for (const _part of relativePath.split(sep)) {
    root = dirname(root);
  }
  return root;
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function isFileSystemError(error: unknown, code: string): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "code" in error &&
    error.code === code
  );
}
