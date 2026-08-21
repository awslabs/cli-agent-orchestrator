#!/usr/bin/env node
// Record a live demo of the CAO MCP Apps fleet UI.
//
// Boots the e2e harness server (e2e/server.mjs) — which serves the *built*
// single-file view bundles inside a real MCP-host iframe with canned fleet state
// and a live SSE feed — then drives Chromium through an operator flow
// (dashboard -> agent detail -> live event stream) while recording video and
// capturing screenshots, and (where gif-capable ffmpeg/gifski exists) exports an
// optimized GIF for the README/docs.
//
// Honest constraint: a true in-host render (Claude Desktop / Cursor / Goose)
// can't be captured headlessly, so this drives the *real* built bundles with
// live host data over the same postMessage/SSE contract the host uses — a
// faithful stand-in for the in-host experience.
//
// Artifacts (docs/media/): mcp-apps-demo.webm, mcp-apps-{dashboard,agent,
// event-stream}.png, and mcp-apps-demo.gif when a gif-capable encoder is found.
//
// Usage:  npm run build:all && node scripts/record-demo.mjs
// Env overrides: CHROMIUM_BIN, FFMPEG_BIN, DEMO_PORT.

import { spawn, spawnSync } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  readdirSync,
  renameSync,
  rmSync,
} from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "@playwright/test";

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(__dirname, "..");
const REPO = resolve(ROOT, "..");
const PORT = Number(process.env.DEMO_PORT ?? 9889);
const BASE = `http://127.0.0.1:${PORT}`;
const OUT_DIR = resolve(REPO, "docs/media");
const TMP_DIR = resolve(ROOT, ".demo-tmp");
const CHROMIUM_BIN = process.env.CHROMIUM_BIN || "/opt/pw-browsers/chromium";
const FFMPEG_BIN =
  process.env.FFMPEG_BIN || "/opt/pw-browsers/ffmpeg-1011/ffmpeg-linux";
const VIEWPORT = { width: 1320, height: 820 };

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitForServer(timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      if ((await fetch(`${BASE}/host.html`)).ok) return;
    } catch {
      /* not up yet */
    }
    await sleep(200);
  }
  throw new Error("e2e harness server did not start");
}

async function emit(event) {
  await fetch(`${BASE}/emit`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ event }),
  }).catch(() => {});
}

function ffmpegHasGif(bin) {
  try {
    const r = spawnSync(bin, ["-hide_banner", "-encoders"], {
      encoding: "utf8",
    });
    return r.status === 0 && /\bgif\b/.test(r.stdout || "");
  } catch {
    return false;
  }
}

function run(bin, args) {
  return new Promise((res, rej) => {
    const p = spawn(bin, args, { stdio: "inherit" });
    p.on("exit", (code) =>
      code === 0 ? res() : rej(new Error(`${bin} exited ${code}`)),
    );
    p.on("error", rej);
  });
}

async function main() {
  if (!existsSync(CHROMIUM_BIN))
    throw new Error(`chromium not found at ${CHROMIUM_BIN}`);
  rmSync(TMP_DIR, { recursive: true, force: true });
  mkdirSync(TMP_DIR, { recursive: true });
  mkdirSync(OUT_DIR, { recursive: true });

  const server = spawn("node", [resolve(ROOT, "e2e/server.mjs")], {
    env: { ...process.env, E2E_PORT: String(PORT) },
    stdio: "inherit",
  });
  try {
    await waitForServer();

    const browser = await chromium.launch({
      executablePath: CHROMIUM_BIN,
      headless: true,
      args: [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--force-color-profile=srgb",
      ],
    });
    const context = await browser.newContext({
      viewport: VIEWPORT,
      recordVideo: { dir: TMP_DIR, size: VIEWPORT },
      deviceScaleFactor: 1,
    });
    const page = await context.newPage();
    const shot = (name) =>
      page.screenshot({ path: resolve(OUT_DIR, `mcp-apps-${name}.png`) });

    // 1) Operator dashboard — supervisor + workers, providers, live statuses.
    await page.goto(`${BASE}/host.html?view=dashboard&fleet=rich`, {
      waitUntil: "domcontentloaded",
    });
    await sleep(4000);
    await shot("dashboard");

    // 2) Drill into the supervisor (agent detail shows its delegation log).
    await page.goto(`${BASE}/host.html?view=agent&fleet=rich`, {
      waitUntil: "domcontentloaded",
    });
    await sleep(3500);
    await shot("agent");

    // 3) Live event stream — push governance events and watch it update.
    await page.goto(`${BASE}/host.html?view=event-stream&fleet=rich`, {
      waitUntil: "domcontentloaded",
    });
    await sleep(900);
    const now = 1750000000000;
    const events = [
      {
        id: "L1",
        kind: "launch",
        terminal_id: "sup-1",
        session: "cao-feature-build",
        ts: now,
      },
      {
        id: "L2",
        kind: "a2a_delegation",
        terminal_id: "sup-1",
        session: "cao-feature-build",
        ts: now + 1,
      },
      {
        id: "L3",
        kind: "launch",
        terminal_id: "dev-1",
        session: "cao-feature-build",
        ts: now + 2,
      },
      {
        id: "L4",
        kind: "file_mod",
        terminal_id: "dev-1",
        session: "cao-feature-build",
        ts: now + 3,
      },
      {
        id: "L5",
        kind: "handoff",
        terminal_id: "sup-1",
        session: "cao-review",
        ts: now + 4,
      },
      {
        id: "L6",
        kind: "a2a_delegation",
        terminal_id: "sup-1",
        session: "cao-review",
        ts: now + 5,
      },
      {
        id: "L7",
        kind: "completion",
        terminal_id: "test-1",
        session: "cao-feature-build",
        ts: now + 6,
      },
      {
        id: "L8",
        kind: "error",
        terminal_id: "doc-1",
        session: "cao-review",
        ts: now + 7,
      },
    ];
    for (const ev of events) {
      await emit(ev);
      await sleep(900);
    }
    await sleep(1200);
    await shot("event-stream");

    await context.close(); // finalizes the .webm
    await browser.close();

    // Persist the webm first so a GIF-conversion failure never loses the capture.
    const webm = readdirSync(TMP_DIR).find((f) => f.endsWith(".webm"));
    if (!webm) throw new Error("no video captured");
    const outWebm = resolve(OUT_DIR, "mcp-apps-demo.webm");
    renameSync(resolve(TMP_DIR, webm), outWebm);
    console.log(`[demo] wrote ${outWebm}`);
    console.log(
      `[demo] wrote screenshots: mcp-apps-{dashboard,agent,event-stream}.png`,
    );

    // Optional GIF — only when a gif-capable encoder is available. Playwright's
    // bundled ffmpeg is a webm-only build (no gif encoder), so this is skipped
    // here and runs on a dev/CI machine with full ffmpeg.
    const outGif = resolve(OUT_DIR, "mcp-apps-demo.gif");
    if (ffmpegHasGif(FFMPEG_BIN)) {
      const palette = resolve(TMP_DIR, "palette.png");
      const vf = "fps=10,scale=900:-1:flags=lanczos";
      await run(FFMPEG_BIN, [
        "-y",
        "-i",
        outWebm,
        "-vf",
        `${vf},palettegen`,
        palette,
      ]);
      await run(FFMPEG_BIN, [
        "-y",
        "-i",
        outWebm,
        "-i",
        palette,
        "-filter_complex",
        `${vf}[x];[x][1:v]paletteuse`,
        outGif,
      ]);
      console.log(`[demo] wrote ${outGif}`);
    } else {
      console.warn(
        `[demo] ${FFMPEG_BIN} has no gif encoder; skipped GIF. ` +
          `Run on a machine with full ffmpeg (or gifski) to produce mcp-apps-demo.gif from the webm.`,
      );
    }
  } finally {
    server.kill("SIGTERM");
    rmSync(TMP_DIR, { recursive: true, force: true });
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
