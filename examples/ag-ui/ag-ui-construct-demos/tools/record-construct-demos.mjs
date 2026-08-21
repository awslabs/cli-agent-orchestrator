#!/usr/bin/env node
// Per-feature shift-left recorder for the AG-UI L2 construct library.
//
// This is BUILD/CI tooling (not part of the shipped library). For each NEW L2
// construct it runs that construct's runnable example (examples/ag-ui/ag-ui-*/run.sh),
// which feeds synthetic / mock AG-UI frames into the construct and ASSERTS its
// behavior — exiting non-zero on ANY drift. The recorder captures the example's
// terminal output, renders it into a terminal-styled page, records Chromium
// playing it back, and exports an optimized GIF to docs/media/ for the PR + docs.
//
// The recording is GATED (this is the shift-left test): if an example exits
// non-zero (a construct regressed) OR does not print its PASS marker, the
// recorder exits non-zero and fails CI. The GIFs are proof-of-work, not
// decoration — a broken construct cannot produce a green recording.
//
// Deterministic + credentials-free: every example runs in offline / synthetic
// (mock) mode — no live provider, network, or secrets required.
//
// Usage:  npm ci && npm run playwright:install && npm run record
// Env:    CAO_REPO (repo root), ONLY=<slug> (record a single feature),
//         FFMPEG_BIN (override the ffmpeg binary; defaults to ffmpeg-static).

import { spawn } from "node:child_process";
import { mkdirSync, readdirSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "@playwright/test";
import ffmpegStatic from "ffmpeg-static";

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO = process.env.CAO_REPO || resolve(__dirname, "..", "..", "..", "..");
const OUT_DIR = resolve(REPO, "docs/media");
const TMP_DIR = resolve(__dirname, ".demo-tmp");
const FFMPEG_BIN = process.env.FFMPEG_BIN || ffmpegStatic || "ffmpeg";
const VIEWPORT = { width: 1000, height: 640 };

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// The four NEW L2 constructs. Each `script` is a runnable example that folds
// synthetic/mock AG-UI frames into the construct and asserts the result,
// printing `pass` on success and exiting non-zero on drift (shift-left gate).
const FEATURES = [
  {
    slug: "ag-ui-supervisor-dashboard",
    title: "SupervisorDashboardStream",
    blurb: "folds STATE_SNAPSHOT / STATE_DELTA into a live fleet view",
    script: "examples/ag-ui/ag-ui-supervisor-dashboard/run.sh",
    pass: "[supervisor-dashboard] PASS",
  },
  {
    slug: "ag-ui-session-timeline",
    title: "MultiAgentSessionTimeline",
    blurb: "reconstructs delegation + message timeline from tool-call frames",
    script: "examples/ag-ui/ag-ui-session-timeline/run.sh",
    pass: "[session-timeline] PASS",
  },
  {
    slug: "ag-ui-handoff-approval",
    title: "AgentHandoffWithApproval",
    blurb: "human-in-the-loop approval: classify, interrupt, resume, expire",
    script: "examples/ag-ui/ag-ui-handoff-approval/run.sh",
    pass: "[handoff-approval] PASS",
  },
  {
    slug: "ag-ui-cross-provider-sync",
    title: "CrossProviderStateSync",
    blurb: "convergent shared state across claude_code / kiro_cli / codex",
    script: "examples/ag-ui/ag-ui-cross-provider-sync/run.sh",
    pass: "[cross-provider-sync] PASS",
  },
];

// Run a feature's example script, capturing combined stdout+stderr. Resolves
// with { code, lines }. Never rejects — the caller enforces the gate.
function runExample(scriptRelPath) {
  return new Promise((res) => {
    const proc = spawn("bash", [resolve(REPO, scriptRelPath)], {
      cwd: REPO,
      env: { ...process.env },
    });
    const lines = [];
    let buf = "";
    const onChunk = (chunk) => {
      buf += chunk.toString();
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        lines.push(buf.slice(0, nl));
        buf = buf.slice(nl + 1);
      }
    };
    proc.stdout.on("data", onChunk);
    proc.stderr.on("data", onChunk);
    proc.on("close", (code) => {
      if (buf.length) lines.push(buf);
      res({ code: code ?? 1, lines });
    });
    proc.on("error", (err) => res({ code: 1, lines: [...lines, `spawn error: ${err.message}`] }));
  });
}

function terminalHtml(feature) {
  // A self-contained terminal-styled page. The recorder appends output lines
  // into #out at a steady cadence (via window.__pushLine) while Chromium records.
  return `<!doctype html><html><head><meta charset="utf-8"><style>
    :root { color-scheme: dark; }
    html,body { margin:0; height:100%; background:#0b0f14; }
    body { font: 14px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; color:#d7e0ea; }
    .win { height:100vh; display:flex; flex-direction:column; }
    .bar { display:flex; align-items:center; gap:8px; padding:8px 12px; background:#141b24; border-bottom:1px solid #223; }
    .dot { width:12px; height:12px; border-radius:50%; }
    .r{background:#ff5f56}.y{background:#ffbd2e}.g{background:#27c93f}
    .title { margin-left:8px; color:#9fb3c8; font-size:12px; }
    .sub { margin-left:auto; color:#5b7085; font-size:11px; }
    #scr { flex:1; overflow:hidden; padding:12px 16px; white-space:pre-wrap; }
    .line { display:block; }
    .pass { color:#27c93f; font-weight:600; }
    .hdr { color:#7cc4ff; }
    .dim { color:#7f93a8; }
    .prompt { color:#27c93f; }
  </style></head><body>
    <div class="win">
      <div class="bar">
        <span class="dot r"></span><span class="dot y"></span><span class="dot g"></span>
        <span class="title">cao · AG-UI shift-left · ${feature.title}</span>
        <span class="sub">${feature.blurb}</span>
      </div>
      <div id="scr"><div id="out"></div></div>
    </div>
    <script>
      const out = document.getElementById('out');
      const scr = document.getElementById('scr');
      window.__pushLine = (text, cls) => {
        const el = document.createElement('span');
        el.className = 'line' + (cls ? ' ' + cls : '');
        el.textContent = text === '' ? '\\u00a0' : text;
        out.appendChild(el);
        scr.scrollTop = scr.scrollHeight;
      };
    </script>
  </body></html>`;
}

function classifyLine(text, passMarker) {
  if (text.includes(passMarker) || /\bPASS\b/.test(text)) return "pass";
  if (/^\[\d\]/.test(text) || text.startsWith("===")) return "hdr";
  if (text.trimStart().startsWith("$")) return "prompt";
  if (text.trimStart().startsWith("    ")) return "dim";
  return "";
}

function run(bin, args) {
  return new Promise((res, rej) => {
    const p = spawn(bin, args, { stdio: "inherit" });
    p.on("exit", (code) => (code === 0 ? res() : rej(new Error(`${bin} exited ${code}`))));
    p.on("error", rej);
  });
}

async function recordFeature(feature) {
  console.log(`\n[record] === ${feature.slug} (${feature.title}) ===`);

  // 1) Run the example. THE GATE: non-zero exit or a missing PASS marker fails.
  const { code, lines } = await runExample(feature.script);
  const passed = lines.some((l) => l.includes(feature.pass));
  console.log(`[record] ${feature.slug}: exit=${code}, pass-marker=${passed}`);
  if (code !== 0) {
    throw new Error(`${feature.slug}: example exited ${code} (construct regressed — shift-left gate)`);
  }
  if (!passed) {
    throw new Error(`${feature.slug}: PASS marker "${feature.pass}" not found (shift-left gate)`);
  }

  // 2) Render the captured run into a terminal video.
  const htmlPath = resolve(TMP_DIR, `${feature.slug}.html`);
  writeFileSync(htmlPath, terminalHtml(feature));

  // Record each feature's video into its OWN dir so we never pick up a
  // previous feature's leftover .webm (readdirSync order is not guaranteed).
  const videoDir = resolve(TMP_DIR, feature.slug);
  mkdirSync(videoDir, { recursive: true });
  const browser = await chromium.launch({
    headless: true,
    args: ["--no-sandbox"],
    ...(process.env.CHROMIUM_BIN ? { executablePath: process.env.CHROMIUM_BIN } : {}),
  });
  const context = await browser.newContext({
    viewport: VIEWPORT,
    recordVideo: { dir: videoDir, size: VIEWPORT },
  });
  const page = await context.newPage();
  await page.goto(`file://${htmlPath}`, { waitUntil: "domcontentloaded" });

  // Intro prompt so the GIF reads like a real terminal session.
  await page.evaluate((s) => window.__pushLine(`$ ./${s}`, "prompt"), feature.script);
  await sleep(500);

  // Cap the number of animated lines so long runs stay a tight, readable GIF.
  const MAX_LINES = 60;
  const shown = lines.length > MAX_LINES ? lines.slice(lines.length - MAX_LINES) : lines;
  for (const text of shown) {
    const cls = classifyLine(text, feature.pass);
    await page.evaluate(({ t, c }) => window.__pushLine(t, c), { t: text, c: cls });
    await sleep(text.trim() === "" ? 50 : 110);
  }
  await sleep(1300); // hold on the final PASS frame

  await context.close(); // finalizes the .webm
  await browser.close();

  // 3) Locate the webm (kept in TMP only) and export an optimized GIF to
  //    docs/media/. Only the GIF is a committed / uploaded artifact — the webm
  //    is an intermediate and is discarded with TMP_DIR at the end.
  const webm = readdirSync(videoDir).find((f) => f.endsWith(".webm"));
  if (!webm) throw new Error(`${feature.slug}: no video captured`);
  const outWebm = resolve(videoDir, `${feature.slug}-demo.webm`);
  renameSync(resolve(videoDir, webm), outWebm);

  const outGif = resolve(OUT_DIR, `${feature.slug}-demo.gif`);
  const palette = resolve(TMP_DIR, `${feature.slug}-palette.png`);
  const vf = "fps=8,scale=720:-1:flags=lanczos";
  await run(FFMPEG_BIN, ["-y", "-i", outWebm, "-vf", `${vf},palettegen`, palette]);
  await run(FFMPEG_BIN, ["-y", "-i", outWebm, "-i", palette, "-lavfi", `${vf} [x]; [x][1:v] paletteuse`, outGif]);
  console.log(`[record] ${feature.slug}: wrote ${outGif}`);
  return { outGif, outWebm };
}

async function main() {
  const only = process.env.ONLY;
  const selected = only ? FEATURES.filter((f) => f.slug === only) : FEATURES;
  if (only && selected.length === 0) {
    throw new Error(`ONLY=${only} did not match any feature slug`);
  }

  rmSync(TMP_DIR, { recursive: true, force: true });
  mkdirSync(TMP_DIR, { recursive: true });
  mkdirSync(OUT_DIR, { recursive: true });

  const done = [];
  for (const feature of selected) {
    const { outGif } = await recordFeature(feature);
    done.push(outGif);
  }

  // Only GIFs remain outside TMP; remove any stray webm from OUT_DIR (legacy).

  rmSync(TMP_DIR, { recursive: true, force: true });
  console.log(`\n[record] PASS: recorded ${done.length} construct demo(s):`);
  for (const g of done) console.log(`  - ${g}`);
}

main().catch((e) => {
  console.error(`[record] FAIL: ${e.message}`);
  process.exit(1);
});
