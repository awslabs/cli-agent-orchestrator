#!/usr/bin/env bun

import {
  advanceLifecycle,
  answerQuestionPacket,
  approveLearningProposal,
  cleanWorktreeMemory,
  confirmDiscovery,
  completeLifecycle,
  PortfolioError,
  checkDispatch,
  createWorktree,
  doctorWorkspace,
  errorMessage,
  initializeWorkspace,
  lifecycleStatus,
  listLearningProposals,
  listQuestionPackets,
  memorySnapshot,
  migrateLifecycle,
  portfolioStatus,
  refreshWorktreeMemory,
  reconcileLearningProposal,
  registerDocument,
  registerLearningProposal,
  rejectLearningProposal,
  submitQuestionPacket,
  updateSession,
  type LearningStatus,
  type MemoryDestination,
  type QuestionAnswerMode,
  validateWorkspace,
  type SessionStatus,
} from "./lib.ts";
import {
  stageHarness,
  syncHarness,
  verifyHarness,
} from "./harness.ts";
import {
  checkConvergence,
  decideConvergenceRisk,
  submitChildResult,
} from "./convergence.ts";

type Options = Record<string, string | boolean>;

const SESSION_STATUSES: SessionStatus[] = [
  "pending",
  "active",
  "waiting",
  "blocked",
  "completed",
  "failed",
];

async function main(argv: string[]): Promise<void> {
  const { command, options } = parseCommand(argv);
  if (command === "help" || options.help) {
    printHelp();
    return;
  }

  const root = required(options, "root");
  switch (command) {
    case "init": {
      const result = await initializeWorkspace(
        root,
        required(options, "id"),
        required(options, "name"),
      );
      printJson({ ok: true, ...result });
      return;
    }
    case "doctor": {
      const result = await doctorWorkspace(root);
      printJson(result);
      if (!result.ok) process.exitCode = 1;
      return;
    }
    case "validate": {
      const result = await validateWorkspace(root);
      printJson(result);
      if (!result.ok) process.exitCode = 1;
      return;
    }
    case "status":
      printJson(await portfolioStatus(root));
      return;
    case "result submit":
      printJson({
        ok: true,
        ...(await submitChildResult(root, required(options, "file"))),
      });
      return;
    case "convergence check": {
      const result = await checkConvergence(root, optional(options, "intent"));
      printJson(result);
      if (!result.ok) process.exitCode = 1;
      return;
    }
    case "convergence decide":
      printJson({
        ok: true,
        ...(await decideConvergenceRisk(
          root,
          required(options, "id"),
          required(options, "decision"),
          required(options, "accepted-by"),
          required(options, "note"),
        )),
      });
      return;
    case "lifecycle migrate":
      printJson({ ok: true, ...(await migrateLifecycle(root)) });
      return;
    case "lifecycle status":
      printJson({ ok: true, ...(await lifecycleStatus(root)) });
      return;
    case "lifecycle advance":
      printJson({
        ok: true,
        ...(await advanceLifecycle(
          root,
          required(options, "to"),
          optional(options, "accepted-by") ?? optional(options, "actor"),
        )),
      });
      return;
    case "lifecycle complete":
      printJson({
        ok: true,
        ...(await completeLifecycle(root, optional(options, "actor"))),
      });
      return;
    case "harness stage":
      printJson({
        ok: true,
        ...(await stageHarness(
          root,
          String(options.provider ?? "claude"),
          required(options, "source"),
          String(
            options["opus-model"] ??
              "global.anthropic.claude-opus-5[1m]",
          ),
        )),
      });
      return;
    case "harness sync":
      printJson({
        ok: true,
        ...(await syncHarness(
          root,
          String(options.provider ?? "claude"),
          optional(options, "project"),
          optional(options, "intent"),
        )),
      });
      return;
    case "harness verify": {
      const result = await verifyHarness(
        root,
        String(options.provider ?? "claude"),
        optional(options, "project"),
        optional(options, "intent"),
      );
      printJson(result);
      if (!result.ok) process.exitCode = 1;
      return;
    }
    case "project register":
      printJson({
        ok: true,
        path: await registerDocument(root, "project", required(options, "file")),
      });
      return;
    case "discovery confirm":
      printJson({
        ok: true,
        ...(await confirmDiscovery(root, required(options, "file"))),
      });
      return;
    case "dependency add":
      printJson({
        ok: true,
        path: await registerDocument(root, "dependency", required(options, "file")),
      });
      return;
    case "intent create":
      printJson({
        ok: true,
        path: await registerDocument(root, "intent", required(options, "file")),
      });
      return;
    case "learning propose":
      printJson({
        ok: true,
        path: await registerLearningProposal(root, required(options, "file")),
      });
      return;
    case "learning list": {
      const status = optional(options, "status") as LearningStatus | undefined;
      if (status && !["pending", "applied", "rejected"].includes(status)) {
        throw new PortfolioError(`invalid learning status: ${status}`);
      }
      printJson({
        ok: true,
        proposals: await listLearningProposals(
          root,
          optional(options, "project"),
          status,
        ),
      });
      return;
    }
    case "learning reconcile":
      printJson({
        ok: true,
        ...(await reconcileLearningProposal(
          root,
          required(options, "id"),
          required(options, "note"),
        )),
      });
      return;
    case "learning approve":
      printJson({
        ok: true,
        ...(await approveLearningProposal(root, required(options, "id"))),
      });
      return;
    case "learning reject":
      await rejectLearningProposal(
        root,
        required(options, "id"),
        required(options, "reason"),
      );
      printJson({ ok: true });
      return;
    case "question submit":
      printJson({
        ok: true,
        ...(await submitQuestionPacket(
          root,
          required(options, "id"),
          required(options, "project"),
          required(options, "intent"),
          required(options, "stage"),
          required(options, "file"),
        )),
      });
      return;
    case "question answer":
      printJson({
        ok: true,
        ...(await answerQuestionPacket(
          root,
          required(options, "id"),
          required(options, "file"),
          questionAnswerMode(options),
          required(options, "answered-by"),
        )),
      });
      return;
    case "question list": {
      const status = optional(options, "status");
      if (status && status !== "pending" && status !== "answered") {
        throw new PortfolioError(`invalid question status: ${status}`);
      }
      printJson({
        ok: true,
        questions: await listQuestionPackets(
          root,
          optional(options, "project"),
          optional(options, "intent"),
          status as "pending" | "answered" | undefined,
        ),
      });
      return;
    }
    case "memory inspect":
      printJson({
        ok: true,
        ...(await memorySnapshot(
          root,
          required(options, "project"),
          required(options, "intent"),
          memoryDestination(options),
          String(options.space ?? "default"),
        )),
      });
      return;
    case "memory refresh":
      printJson({
        ok: true,
        ...(await refreshWorktreeMemory(
          root,
          required(options, "project"),
          required(options, "intent"),
          memoryDestination(options),
          required(options, "expected-worktree-revision"),
          String(options.space ?? "default"),
        )),
      });
      return;
    case "memory clean":
      printJson({
        ok: true,
        ...(await cleanWorktreeMemory(
          root,
          required(options, "project"),
          required(options, "intent"),
          memoryDestination(options),
          required(options, "expected-worktree-revision"),
          String(options.space ?? "default"),
        )),
      });
      return;
    case "worktree create":
      printJson({
        ok: true,
        worktree: await createWorktree(
          root,
          required(options, "project"),
          required(options, "intent"),
          required(options, "branch"),
          String(options.base ?? "main"),
        ),
      });
      return;
    case "dispatch check":
      printJson({
        ok: true,
        ...(await checkDispatch(
          root,
          required(options, "project"),
          required(options, "intent"),
        )),
      });
      return;
    case "session update": {
      const status = required(options, "status") as SessionStatus;
      if (!SESSION_STATUSES.includes(status)) {
        throw new PortfolioError(`invalid session status: ${status}`);
      }
      await updateSession(
        root,
        required(options, "intent"),
        required(options, "project"),
        status,
        optional(options, "terminal"),
      );
      printJson({ ok: true });
      return;
    }
    default:
      throw new PortfolioError(`unknown command: ${command}`);
  }
}

function parseCommand(argv: string[]): { command: string; options: Options } {
  const tokens = [...argv];
  if (tokens.length === 0) return { command: "help", options: {} };

  const first = tokens.shift()!;
  const nested = new Set([
    "project",
    "discovery",
    "dependency",
    "intent",
    "learning",
    "harness",
    "lifecycle",
    "result",
    "convergence",
    "question",
    "memory",
    "worktree",
    "dispatch",
    "session",
  ]);
  const command = nested.has(first) ? `${first} ${tokens.shift() ?? ""}`.trim() : first;
  const options: Options = {};

  while (tokens.length > 0) {
    const token = tokens.shift()!;
    if (!token.startsWith("--")) {
      throw new PortfolioError(`unexpected argument: ${token}`);
    }
    const key = token.slice(2);
    const next = tokens[0];
    options[key] = next && !next.startsWith("--") ? tokens.shift()! : true;
  }
  return { command, options };
}

function required(options: Options, key: string): string {
  const value = options[key];
  if (typeof value !== "string" || !value) {
    throw new PortfolioError(`--${key} is required`);
  }
  return value;
}

function optional(options: Options, key: string): string | undefined {
  const value = options[key];
  return typeof value === "string" && value ? value : undefined;
}

function memoryDestination(options: Options): MemoryDestination {
  const value = String(options.destination ?? "project");
  if (value !== "project" && value !== "team") {
    throw new PortfolioError(`invalid memory destination: ${value}`);
  }
  return value;
}

function questionAnswerMode(options: Options): QuestionAnswerMode {
  const value = String(options.mode ?? "");
  if (value !== "guided" && value !== "markdown" && value !== "chat") {
    throw new PortfolioError(
      "question answer --mode must be guided, markdown, or chat",
    );
  }
  return value;
}

function printJson(value: unknown): void {
  console.log(JSON.stringify(value, null, 2));
}

function printHelp(): void {
  console.log(`AI-DLC Portfolio

Usage:
  portfolio.ts init --root PATH --id ID --name NAME
  portfolio.ts doctor --root PATH
  portfolio.ts validate --root PATH
  portfolio.ts status --root PATH
  portfolio.ts result submit --root PATH --file FILE
  portfolio.ts convergence check --root PATH [--intent ID]
  portfolio.ts convergence decide --root PATH --id ID --decision accepted|resolved --accepted-by HUMAN --note TEXT
  portfolio.ts lifecycle migrate --root PATH
  portfolio.ts lifecycle status --root PATH
  portfolio.ts lifecycle advance --root PATH --to PHASE [--accepted-by HUMAN] [--actor NAME]
  portfolio.ts lifecycle complete --root PATH [--actor NAME]
  portfolio.ts harness stage --root PATH --source PATH [--provider claude] [--opus-model MODEL]
  portfolio.ts harness sync --root PATH [--provider claude] [--project ID] [--intent ID]
  portfolio.ts harness verify --root PATH [--provider claude] [--project ID] [--intent ID]
  portfolio.ts project register --root PATH --file FILE
  portfolio.ts discovery confirm --root PATH --file FILE
  portfolio.ts dependency add --root PATH --file FILE
  portfolio.ts intent create --root PATH --file FILE
  portfolio.ts learning propose --root PATH --file FILE
  portfolio.ts learning list --root PATH [--project ID] [--status STATUS]
  portfolio.ts learning reconcile --root PATH --id ID --note TEXT
  portfolio.ts learning approve --root PATH --id ID
  portfolio.ts learning reject --root PATH --id ID --reason TEXT
  portfolio.ts question submit --root PATH --id ID --project ID --intent ID --stage ID --file FILE
  portfolio.ts question answer --root PATH --id ID --file FILE --mode guided|markdown|chat --answered-by NAME
  portfolio.ts question list --root PATH [--project ID] [--intent ID] [--status pending|answered]
  portfolio.ts memory inspect --root PATH --project ID --intent ID [--destination project|team] [--space ID]
  portfolio.ts memory refresh --root PATH --project ID --intent ID --expected-worktree-revision HASH [--destination project|team] [--space ID]
  portfolio.ts memory clean --root PATH --project ID --intent ID --expected-worktree-revision HASH [--destination project|team] [--space ID]
  portfolio.ts worktree create --root PATH --project ID --intent ID --branch NAME [--base REF]
  portfolio.ts dispatch check --root PATH --project ID --intent ID
  portfolio.ts session update --root PATH --project ID --intent ID --status STATUS [--terminal ID]
`);
}

main(Bun.argv.slice(2)).catch((error) => {
  console.error(JSON.stringify({ ok: false, error: errorMessage(error) }, null, 2));
  process.exitCode = 1;
});
