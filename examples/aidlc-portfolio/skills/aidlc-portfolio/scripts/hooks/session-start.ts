#!/usr/bin/env bun

import {
  doctorWorkspace,
  errorMessage,
  findPortfolioRoot,
  portfolioStatus,
} from "../lib.ts";

interface HookInput {
  cwd?: string;
}

async function main(): Promise<void> {
  const raw = await Bun.stdin.text();
  const input = raw.trim() ? (JSON.parse(raw) as HookInput) : {};
  const root = await findPortfolioRoot(input.cwd ?? process.cwd());
  if (!root) {
    console.log(JSON.stringify({ ok: true, portfolio: null }));
    return;
  }

  const doctor = await doctorWorkspace(root);
  if (!doctor.ok) {
    console.log(JSON.stringify({ ok: false, root, errors: doctor.errors }));
    process.exitCode = 2;
    return;
  }

  const status = await portfolioStatus(root);
  console.log(
    JSON.stringify({
      ok: true,
      root,
      status,
      hookSpecificOutput: {
        hookEventName: "SessionStart",
        additionalContext: `AI-DLC portfolio ${status.portfolio.id} is available at ${root}. Validate dispatches before launching child runners.`,
      },
    }),
  );
}

main().catch((error) => {
  console.error(JSON.stringify({ ok: false, error: errorMessage(error) }));
  process.exitCode = 2;
});

