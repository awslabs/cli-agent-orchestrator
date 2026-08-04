import Ajv2020, { type ErrorObject } from "ajv/dist/2020.js";
import { createHash } from "node:crypto";
import {
  mkdir,
  readFile,
  readdir,
  rename,
  rm,
  writeFile,
} from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { parse, stringify } from "yaml";

type Disposition = "satisfied" | "blocked" | "deferred" | "unknown";
type EffectiveDisposition = Disposition;

interface ChildResult {
  schemaVersion: 1;
  project: string;
  intent: string;
  changedComponents: string[];
  changedCapabilities: string[];
  contracts: Array<{
    dependency: string;
    change: "compatible" | "breaking" | "unknown";
    evidence: string[];
  }>;
  dependencyAssumptions: Array<{
    dependency: string;
    disposition: Disposition;
    note: string;
  }>;
  verification: string[];
  submittedAt: string;
}

interface ProjectDocument {
  id: string;
  businessCapabilities: string[];
  components: Array<{ id: string }>;
}

interface DependencyDocument {
  id: string;
  type:
    | "runtime"
    | "contract"
    | "data"
    | "build"
    | "deployment"
    | "operational"
    | "business"
    | "release";
  source: { project: string; component?: string };
  target: { project: string; component?: string };
  blockingAt: string[];
  status: string;
}

interface IntentDocument {
  id: string;
  projects: Array<{ project: string }>;
}

interface PortfolioDocument {
  businessCapabilities: string[];
}

interface PortfolioState {
  sessions: Record<string, { status: string }>;
}

interface ConvergenceDecision {
  schemaVersion: 1;
  id: string;
  riskRevision: string;
  disposition: "accepted" | "resolved";
  acceptedBy: string;
  note: string;
  decidedAt: string;
}

export interface ConvergenceRisk {
  id: string;
  revision: string;
  intent: string;
  project: string;
  dependency?: string;
  category:
    | "missing-result"
    | "unknown-component"
    | "unknown-capability"
    | "missing-assumption"
    | "dependency-assumption"
    | "contract-change"
    | "dependency-order";
  disposition: Disposition;
  effectiveDisposition: EffectiveDisposition;
  message: string;
  decidable: boolean;
  decision: ConvergenceDecision | null;
}

export interface ConvergenceReport {
  ok: boolean;
  intents: string[];
  results: Array<{
    intent: string;
    project: string;
    present: boolean;
    path: string;
  }>;
  affected: Record<
    string,
    { upstream: string[]; downstream: string[] }
  >;
  relationships: Array<{
    id: string;
    type: DependencyDocument["type"];
    source: string;
    target: string;
    blockingAt: string[];
    status: EffectiveDisposition;
    risks: string[];
  }>;
  risks: ConvergenceRisk[];
  summary: Record<EffectiveDisposition, number>;
}

interface LoadedPortfolio {
  root: string;
  portfolio: PortfolioDocument;
  projects: ProjectDocument[];
  dependencies: DependencyDocument[];
  intents: IntentDocument[];
  state: PortfolioState;
}

export class ConvergenceError extends Error {}

export async function submitChildResult(
  rootInput: string,
  sourceFile: string,
): Promise<{ path: string; changed: boolean }> {
  const root = resolve(rootInput);
  const result = await readYaml<ChildResult>(resolve(sourceFile));
  await validateChildResult(result);
  const loaded = await loadPortfolio(root);
  validateResultReferences(result, loaded);

  const path = resultPath(root, result.intent, result.project);
  const content = stringify(result);
  const previous = await optionalRead(path);
  if (previous === content) {
    return { path, changed: false };
  }
  await atomicWrite(path, content);
  return { path, changed: true };
}

export async function checkConvergence(
  rootInput: string,
  intentFilter?: string,
): Promise<ConvergenceReport> {
  const root = resolve(rootInput);
  const loaded = await loadPortfolio(root);
  const intents = loaded.intents.filter(
    (intent) => !intentFilter || intent.id === intentFilter,
  );
  if (intents.length === 0) {
    throw new ConvergenceError(
      intentFilter
        ? `portfolio intent is not registered: ${intentFilter}`
        : "portfolio has no registered intents",
    );
  }

  const decisions = await readDecisions(root);
  const risks: ConvergenceRisk[] = [];
  const results: ConvergenceReport["results"] = [];
  const affected: ConvergenceReport["affected"] = {};
  const relevantDependencyIds = new Set<string>();

  for (const intent of intents) {
    const intentProjects = new Set(intent.projects.map((mapping) => mapping.project));
    const intentDependencies = loaded.dependencies.filter(
      (dependency) =>
        intentProjects.has(dependency.source.project) &&
        intentProjects.has(dependency.target.project),
    );
    for (const dependency of intentDependencies) {
      relevantDependencyIds.add(dependency.id);
    }

    for (const mapping of intent.projects) {
      const path = resultPath(root, intent.id, mapping.project);
      const result = await optionalChildResult(path);
      results.push({
        intent: intent.id,
        project: mapping.project,
        present: result !== null,
        path,
      });
      affected[`${intent.id}:${mapping.project}`] = {
        upstream: traverseProjects(
          mapping.project,
          loaded.dependencies,
          "upstream",
        ),
        downstream: traverseProjects(
          mapping.project,
          loaded.dependencies,
          "downstream",
        ),
      };
      if (!result) {
        const dependencies = intentDependencies.filter(
          (dependency) =>
            dependency.source.project === mapping.project ||
            dependency.target.project === mapping.project,
        );
        const riskDependencies =
          dependencies.length > 0 ? dependencies : [undefined];
        for (const dependency of riskDependencies) {
          risks.push(createRisk({
            intent: intent.id,
            project: mapping.project,
            dependency: dependency?.id,
            category: "missing-result",
            disposition: "blocked",
            message: `completed child ${intent.id}:${mapping.project} has no structured result`,
            decidable: false,
          }));
        }
        continue;
      }
      validateResultReferences(result, loaded);

      const project = loaded.projects.find((item) => item.id === mapping.project)!;
      for (const component of result.changedComponents) {
        if (!project.components.some((item) => item.id === component)) {
          risks.push(
            createRisk({
              intent: intent.id,
              project: mapping.project,
              category: "unknown-component",
              disposition: "unknown",
              message: `changed component ${component} is not registered in project ${mapping.project}`,
              decidable: true,
              suffix: component,
            }),
          );
        }
      }
      for (const capability of result.changedCapabilities) {
        if (
          !project.businessCapabilities.includes(capability) &&
          !loaded.portfolio.businessCapabilities.includes(capability)
        ) {
          risks.push(
            createRisk({
              intent: intent.id,
              project: mapping.project,
              category: "unknown-capability",
              disposition: "unknown",
              message: `changed capability ${capability} is not registered`,
              decidable: true,
              suffix: capability,
            }),
          );
        }
      }

      const relevantDependencies = intentDependencies.filter(
        (dependency) =>
          (dependency.source.project === mapping.project ||
            dependency.target.project === mapping.project),
      );
      for (const dependency of relevantDependencies) {
        const assumption = result.dependencyAssumptions.find(
          (item) => item.dependency === dependency.id,
        );
        if (!assumption) {
          risks.push(
            createRisk({
              intent: intent.id,
              project: mapping.project,
              dependency: dependency.id,
              category: "missing-assumption",
              disposition: "unknown",
              message: `child result does not state an assumption for ${dependency.id}`,
              decidable: true,
            }),
          );
        } else if (assumption.disposition !== "satisfied") {
          risks.push(
            createRisk({
              intent: intent.id,
              project: mapping.project,
              dependency: dependency.id,
              category: "dependency-assumption",
              disposition: assumption.disposition,
              message: assumption.note,
              decidable: true,
            }),
          );
        }

        if (
          dependency.source.project === mapping.project &&
          dependency.blockingAt.some((point) =>
            ["integration", "release"].includes(point),
          )
        ) {
          const targetStatus =
            loaded.state.sessions[`${intent.id}:${dependency.target.project}`]
              ?.status;
          if (targetStatus !== "completed") {
            risks.push(
              createRisk({
                intent: intent.id,
                project: mapping.project,
                dependency: dependency.id,
                category: "dependency-order",
                disposition: "blocked",
                message: `${dependency.source.project} waits for ${dependency.target.project} at ${dependency.blockingAt.join(", ")}`,
                decidable: false,
              }),
            );
          }
        }
      }

      for (const contract of result.contracts) {
        relevantDependencyIds.add(contract.dependency);
        if (contract.change !== "compatible") {
          risks.push(
            createRisk({
              intent: intent.id,
              project: mapping.project,
              dependency: contract.dependency,
              category: "contract-change",
              disposition: contract.change === "breaking" ? "blocked" : "unknown",
              message: `${contract.change} contract change reported for ${contract.dependency}`,
              decidable: true,
            }),
          );
        }
      }
    }
  }

  const appliedRisks = risks.map((risk) => applyDecision(risk, decisions));
  const relationships = loaded.dependencies
    .filter((dependency) => relevantDependencyIds.has(dependency.id))
    .map((dependency) => {
      const relationshipRisks = appliedRisks.filter(
        (risk) => risk.dependency === dependency.id,
      );
      return {
        id: dependency.id,
        type: dependency.type,
        source: dependency.source.project,
        target: dependency.target.project,
        blockingAt: dependency.blockingAt,
        status: worstDisposition(
          relationshipRisks.map((risk) => risk.effectiveDisposition),
        ),
        risks: relationshipRisks.map((risk) => risk.id),
      };
    });
  const summary: ConvergenceReport["summary"] = {
    satisfied: 0,
    blocked: 0,
    deferred: 0,
    unknown: 0,
  };
  for (const relationship of relationships) summary[relationship.status] += 1;
  for (const risk of appliedRisks.filter((item) => !item.dependency)) {
    summary[risk.effectiveDisposition] += 1;
  }
  const unresolved = appliedRisks.filter(
    (risk) =>
      risk.effectiveDisposition !== "satisfied" &&
      !(risk.effectiveDisposition === "deferred" && risk.decision),
  );
  return {
    ok: unresolved.length === 0,
    intents: intents.map((intent) => intent.id),
    results,
    affected,
    relationships,
    risks: appliedRisks,
    summary,
  };
}

export async function decideConvergenceRisk(
  rootInput: string,
  id: string,
  disposition: string,
  acceptedBy: string,
  note: string,
): Promise<{ path: string; changed: boolean }> {
  if (disposition !== "accepted" && disposition !== "resolved") {
    throw new ConvergenceError(
      "convergence decision must be accepted or resolved",
    );
  }
  if (!acceptedBy.trim() || !note.trim()) {
    throw new ConvergenceError(
      "convergence decision requires accepted-by and note",
    );
  }
  const root = resolve(rootInput);
  const report = await checkConvergence(root);
  const risk = report.risks.find((item) => item.id === id);
  if (!risk) {
    throw new ConvergenceError(`convergence risk is not current: ${id}`);
  }
  if (!risk.decidable) {
    throw new ConvergenceError(`convergence risk requires remediation: ${id}`);
  }
  const decision: ConvergenceDecision = {
    schemaVersion: 1,
    id,
    riskRevision: risk.revision,
    disposition,
    acceptedBy: acceptedBy.trim(),
    note: note.trim(),
    decidedAt: new Date().toISOString(),
  };
  const path = decisionPath(root, id);
  const content = stringify(decision);
  const previous = await optionalRead(path);
  if (previous) {
    const existing = parse(previous) as ConvergenceDecision;
    if (
      existing.riskRevision === decision.riskRevision &&
      existing.disposition === decision.disposition &&
      existing.acceptedBy === decision.acceptedBy &&
      existing.note === decision.note
    ) {
      return { path, changed: false };
    }
  }
  await atomicWrite(path, content);
  return { path, changed: true };
}

function validateResultReferences(
  result: ChildResult,
  loaded: LoadedPortfolio,
): void {
  const project = loaded.projects.find((item) => item.id === result.project);
  const intent = loaded.intents.find((item) => item.id === result.intent);
  if (!project || !intent?.projects.some((item) => item.project === result.project)) {
    throw new ConvergenceError(
      `project ${result.project} is not registered in intent ${result.intent}`,
    );
  }
  const dependencies = new Map(
    loaded.dependencies.map((dependency) => [dependency.id, dependency]),
  );
  assertUniqueReferences(
    result.contracts.map((item) => item.dependency),
    "contract",
  );
  assertUniqueReferences(
    result.dependencyAssumptions.map((item) => item.dependency),
    "dependency assumption",
  );
  for (const contract of result.contracts) {
    const dependency = dependencies.get(contract.dependency);
    if (!dependency || dependency.type !== "contract") {
      throw new ConvergenceError(
        `contract ${contract.dependency} is not a registered contract dependency`,
      );
    }
    requireProjectEndpoint(dependency, result.project);
  }
  for (const assumption of result.dependencyAssumptions) {
    const dependency = dependencies.get(assumption.dependency);
    if (!dependency) {
      throw new ConvergenceError(
        `dependency assumption references unknown dependency ${assumption.dependency}`,
      );
    }
    requireProjectEndpoint(dependency, result.project);
  }
}

function requireProjectEndpoint(
  dependency: DependencyDocument,
  project: string,
): void {
  if (
    dependency.source.project !== project &&
    dependency.target.project !== project
  ) {
    throw new ConvergenceError(
      `dependency ${dependency.id} does not involve project ${project}`,
    );
  }
}

function assertUniqueReferences(values: string[], label: string): void {
  if (values.length !== new Set(values).size) {
    throw new ConvergenceError(`${label} entries must be unique`);
  }
}

function createRisk(input: {
  intent: string;
  project: string;
  dependency?: string;
  category: ConvergenceRisk["category"];
  disposition: Disposition;
  message: string;
  decidable: boolean;
  suffix?: string;
}): ConvergenceRisk {
  const identity = [
    input.intent,
    input.project,
    input.dependency,
    input.category,
    input.suffix,
  ];
  const id = `${input.intent}-${input.project}-${input.category}-${revisionOf(
    identity,
  ).slice(0, 12)}`;
  const core = {
    id,
    intent: input.intent,
    project: input.project,
    dependency: input.dependency,
    category: input.category,
    disposition: input.disposition,
    message: input.message,
    decidable: input.decidable,
  };
  return {
    ...core,
    revision: revisionOf(core),
    effectiveDisposition: input.disposition,
    decision: null,
  };
}

function applyDecision(
  risk: ConvergenceRisk,
  decisions: Map<string, ConvergenceDecision>,
): ConvergenceRisk {
  const decision = decisions.get(risk.id);
  if (!decision || decision.riskRevision !== risk.revision) return risk;
  return {
    ...risk,
    effectiveDisposition:
      decision.disposition === "resolved" ? "satisfied" : "deferred",
    decision,
  };
}

function worstDisposition(
  values: EffectiveDisposition[],
): EffectiveDisposition {
  const order: EffectiveDisposition[] = [
    "blocked",
    "unknown",
    "deferred",
    "satisfied",
  ];
  return order.find((value) => values.includes(value)) ?? "satisfied";
}

function traverseProjects(
  start: string,
  dependencies: DependencyDocument[],
  direction: "upstream" | "downstream",
): string[] {
  const visited = new Set<string>([start]);
  const queue = [start];
  while (queue.length > 0) {
    const current = queue.shift()!;
    for (const dependency of dependencies) {
      const from =
        direction === "upstream"
          ? dependency.source.project
          : dependency.target.project;
      const to =
        direction === "upstream"
          ? dependency.target.project
          : dependency.source.project;
      if (from === current && !visited.has(to)) {
        visited.add(to);
        queue.push(to);
      }
    }
  }
  visited.delete(start);
  return [...visited].sort();
}

async function loadPortfolio(root: string): Promise<LoadedPortfolio> {
  const portfolio = await readYaml<PortfolioDocument>(
    join(root, "portfolio/portfolio.yaml"),
  );
  const projects = await readYamlDirectory<ProjectDocument>(
    join(root, "portfolio/projects"),
  );
  const dependencies = await readYamlDirectory<DependencyDocument>(
    join(root, "portfolio/dependencies"),
  );
  const intents = await readYamlDirectory<IntentDocument>(
    join(root, "portfolio/intents"),
  );
  const state = JSON.parse(
    await readFile(join(root, "portfolio/state.json"), "utf8"),
  ) as PortfolioState;
  return { root, portfolio, projects, dependencies, intents, state };
}

async function readYamlDirectory<T>(directory: string): Promise<T[]> {
  const names = (await readdir(directory))
    .filter((name) => name.endsWith(".yaml") || name.endsWith(".yml"))
    .sort();
  return Promise.all(names.map((name) => readYaml<T>(join(directory, name))));
}

async function readDecisions(
  root: string,
): Promise<Map<string, ConvergenceDecision>> {
  const directory = join(root, "portfolio/convergence-decisions");
  let names: string[];
  try {
    names = (await readdir(directory))
      .filter((name) => name.endsWith(".yaml") || name.endsWith(".yml"))
      .sort();
  } catch {
    return new Map();
  }
  const decisions = await Promise.all(
    names.map((name) => readYaml<ConvergenceDecision>(join(directory, name))),
  );
  return new Map(decisions.map((decision) => [decision.id, decision]));
}

async function optionalChildResult(path: string): Promise<ChildResult | null> {
  try {
    const result = await readYaml<ChildResult>(path);
    await validateChildResult(result);
    return result;
  } catch (error) {
    if (isMissingPathError(error)) return null;
    throw error;
  }
}

async function validateChildResult(result: ChildResult): Promise<void> {
  const schemaPath = new URL(
    "../assets/schemas/child-result.schema.json",
    import.meta.url,
  );
  const schema = JSON.parse(await readFile(schemaPath, "utf8"));
  const validate = new Ajv2020({ allErrors: true, strict: true }).compile(schema);
  if (!validate(result)) {
    throw new ConvergenceError(
      formatSchemaErrors("child result", validate.errors),
    );
  }
}

function resultPath(root: string, intent: string, project: string): string {
  return join(root, "portfolio/results", `${intent}--${project}.yaml`);
}

function decisionPath(root: string, id: string): string {
  if (!/^[a-z0-9][a-z0-9-]*$/.test(id)) {
    throw new ConvergenceError(`invalid convergence risk id: ${id}`);
  }
  return join(root, "portfolio/convergence-decisions", `${id}.yaml`);
}

async function readYaml<T>(path: string): Promise<T> {
  try {
    return parse(await readFile(path, "utf8")) as T;
  } catch (error) {
    if (isMissingPathError(error)) throw error;
    throw new ConvergenceError(
      `cannot read YAML ${path}: ${errorMessage(error)}`,
    );
  }
}

async function optionalRead(path: string): Promise<string | null> {
  try {
    return await readFile(path, "utf8");
  } catch (error) {
    if (isMissingPathError(error)) return null;
    throw error;
  }
}

async function atomicWrite(path: string, content: string): Promise<void> {
  await mkdir(dirname(path), { recursive: true });
  const temporary = `${path}.${crypto.randomUUID()}.tmp`;
  try {
    await writeFile(temporary, content, { flag: "wx" });
    await rename(temporary, path);
  } finally {
    await rm(temporary, { force: true });
  }
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

function revisionOf(value: unknown): string {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}

function isMissingPathError(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "code" in error &&
    error.code === "ENOENT"
  );
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
