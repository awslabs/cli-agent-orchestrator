#!/usr/bin/env bun

import { checkDispatch, errorMessage } from "../lib.ts";

interface GuardInput {
  root: string;
  project: string;
  intent: string;
}

async function main(): Promise<void> {
  const raw = await Bun.stdin.text();
  if (!raw.trim()) {
    throw new Error("dispatch guard requires JSON input");
  }
  const input = JSON.parse(raw) as GuardInput;
  const dispatch = await checkDispatch(input.root, input.project, input.intent);
  console.log(JSON.stringify({ ok: true, ...dispatch }));
}

main().catch((error) => {
  console.error(JSON.stringify({ ok: false, error: errorMessage(error) }));
  process.exitCode = 2;
});
