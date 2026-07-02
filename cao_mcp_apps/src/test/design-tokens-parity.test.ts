// Design-token + status SSOT parity guard.
//
// Re-runs the codegen in --check mode. If any committed generated artifact
// (web/tailwind.preset.cjs, web/src/status.generated.ts,
// cao_mcp_apps/src/shared/tokens.generated.css,
// cao_mcp_apps/src/shared/status.generated.ts) is stale relative to the SSOT
// (design-tokens/tokens.json + status.json), `gen.mjs --check` exits non-zero
// and this test fails with the regen instructions in the output.

import { execFileSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const HERE = dirname(fileURLToPath(import.meta.url));
// cao_mcp_apps/src/test -> repo root
const REPO = resolve(HERE, "..", "..", "..");
const GEN = resolve(REPO, "design-tokens", "gen.mjs");

describe("design-token + status SSOT parity", () => {
  it("generated artifacts are up to date (node design-tokens/gen.mjs --check)", () => {
    let output = "";
    let code = 0;
    try {
      output = execFileSync("node", [GEN, "--check"], {
        cwd: REPO,
        encoding: "utf8",
      });
    } catch (err: unknown) {
      const e = err as { status?: number; stdout?: string; stderr?: string };
      code = e.status ?? 1;
      output = `${e.stdout ?? ""}${e.stderr ?? ""}`;
    }
    expect(output).toContain("all artifacts up to date");
    expect(code).toBe(0);
  });
});
