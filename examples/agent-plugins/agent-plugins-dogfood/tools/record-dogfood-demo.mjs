#!/usr/bin/env node
// Shift-left dog-food recorder for the CAO Agent Plugins pipeline.
//
// This is BUILD/CI tooling (not part of the shipped feature). It runs the
// asserting example examples/agent-plugins/agent-plugins-dogfood/run.sh, which
// drives CAO installing its OWN `cao` package through its OWN new agent-plugin
// pipeline — validate -> add -> install (Kiro + OpenCode) -> remove — and
// ASSERTS the R1 delivery fix, the Finding 2 collision guard, and cross-provider
// removal, exiting non-zero on ANY drift. The recorder captures the run's
// terminal output, renders it into a terminal-styled page, records Chromium
// playing it back, and exports an optimized GIF to docs/media/ for the PR + docs.
//
// The recording is GATED (this is the shift-left test): if the example exits
// non-zero (the pipeline regressed) OR does not print its PASS marker, the
// recorder exits non-zero and fails CI. The GIF is proof-of-work, not
// decoration — a broken pipeline cannot produce a green recording.
//
// Deterministic + credentials-free: the example runs OFFLINE by default (no
// provider binary, network, or secrets) under a scratch HOME/CAO_HOME_DIR, so
// no real config is read or written and no real home path renders. The LIVE
// OpenCode observational step (CAO_DOGFOOD_LIVE=1) needs the `opencode` binary
// and is therefore excluded from CI; CI records the offline path.
//
// Usage:  npm install && npm run playwright:install && npm run record
// Env:    CAO_REPO (repo root), FFMPEG_BIN (override ffmpeg; defaults to ffmpeg-static),
//         CAO_DOGFOOD_LIVE=1 (include the live OpenCode observational step, needs opencode).

import { spawn } from "node:child_process";
import { mkdirSync, readdirSync, renameSync, rmSync, statSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "@playwright/test";
import ffmpegStatic from "ffmpeg-static";

const __dirname = dirname(fileURLToPath(import.meta.url));
// tools/ is four levels below the repo root:
// examples/agent-plugins/agent-plugins-dogfood/tools -> repo.
const REPO = process.env.CAO_REPO || resolve(__dirname, "..", "..", "..", "..");
const OUT_DIR = resolve(REPO, "docs/media");
const TMP_DIR = resolve(__dirname, ".demo-tmp");
const FFMPEG_BIN = process.env.FFMPEG_BIN || ffmpegStatic || "ffmpeg";
const VIEWPORT = { width: 1000, height: 640 };

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const FEATURE = {
  slug: "agent-plugins-dogfood",
  title: "Agent Plugins dog-food",
  blurb: "CAO installs its own `cao` package through its own plugin pipeline",
  script: "examples/agent-plugins/agent-plugins-dogfood/run.sh",
  pass: "[agent-plugins-dogfood] PASS",
};

// Run the example script, capturing combined stdout+stderr. Resolves with
// { code, lines }. Never rejects — the caller enforces the gate. TMPDIR=/tmp so
// the scratch paths rendered on screen are the clean /tmp form on every OS.
function runExample(scriptRelPath) {
  return new Promise((res) => {
    const proc = spawn("bash", [resolve(REPO, scriptRelPath)], {
      cwd: REPO,
      env: { ...process.env, CAO_REPO: REPO, TMPDIR: process.env.TMPDIR || "/tmp" },
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
        <span class="title">cao · Agent Plugins shift-left · ${feature.title}</span>
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
  if (text.startsWith("$")) return "prompt";
  if (/^\s/.test(text)) return "dim";
  return "";
}

function run(bin, args) {
  return new Promise((res, rej) => {
    const p = spawn(bin, args, { stdio: "inherit" });
    p.on("exit", (code) => (code === 0 ? res() : rej(new Error(`${bin} exited ${code}`))));
    p.on("error", rej);
  });
}

async function main() {
  rmSync(TMP_DIR, { recursive: true, force: true });
  mkdirSync(TMP_DIR, { recursive: true });
  mkdirSync(OUT_DIR, { recursive: true });

  const feature = FEATURE;
  console.log(`\n[record] === ${feature.slug} (${feature.title}) ===`);

  // 1) Run the example. THE GATE: non-zero exit or a missing PASS marker fails.
  const { code, lines } = await runExample(feature.script);
  const passed = lines.some((l) => l.includes(feature.pass));
  console.log(`[record] ${feature.slug}: exit=${code}, pass-marker=${passed}`);
  if (code !== 0) {
    throw new Error(
      `${feature.slug}: example exited ${code} (pipeline regressed — shift-left gate)`
    );
  }
  if (!passed) {
    throw new Error(`${feature.slug}: PASS marker "${feature.pass}" not found (shift-left gate)`);
  }

  // 2) Render the captured run into a terminal video.
  const htmlPath = resolve(TMP_DIR, `${feature.slug}.html`);
  writeFileSync(htmlPath, terminalHtml(feature));

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

  await page.evaluate((s) => window.__pushLine(`$ ./${s}`, "prompt"), feature.script);
  await sleep(400);

  const MAX_LINES = 60;
  const shown = lines.length > MAX_LINES ? lines.slice(lines.length - MAX_LINES) : lines;
  for (const text of shown) {
    const cls = classifyLine(text, feature.pass);
    await page.evaluate(({ t, c }) => window.__pushLine(t, c), { t: text, c: cls });
    await sleep(text.trim() === "" ? 40 : 90);
  }
  await sleep(1100); // hold on the final PASS frame

  await context.close(); // finalizes the .webm
  await browser.close();

  // 3) Export an optimized GIF to docs/media/. Only the GIF is committed; the
  //    webm is an intermediate discarded with TMP_DIR.
  const webm = readdirSync(videoDir).find((f) => f.endsWith(".webm"));
  if (!webm) throw new Error(`${feature.slug}: no video captured`);
  const outWebm = resolve(videoDir, `${feature.slug}-demo.webm`);
  renameSync(resolve(videoDir, webm), outWebm);

  const outGif = resolve(OUT_DIR, `${feature.slug}-demo.gif`);
  const palette = resolve(TMP_DIR, `${feature.slug}-palette.png`);
  // Flat terminal text compresses far better with a small palette and NO
  // dithering (the default error-diffusion dither adds per-pixel noise that
  // defeats GIF's run-length compression and can triple the file). fps=7 +
  // scale=680 keep it readable while staying within the repo's 300 KB - 2.1 MB
  // committed-GIF range.
  const vf = "fps=7,scale=680:-1:flags=lanczos";
  await run(FFMPEG_BIN, ["-y", "-i", outWebm, "-vf", `${vf},palettegen=max_colors=64`, palette]);
  await run(FFMPEG_BIN, [
    "-y", "-i", outWebm, "-i", palette,
    "-lavfi", `${vf} [x]; [x][1:v] paletteuse=dither=none`,
    outGif,
  ]);

  const bytes = statSync(outGif).size;
  console.log(`[record] ${feature.slug}: wrote ${outGif} (${bytes} bytes)`);

  rmSync(TMP_DIR, { recursive: true, force: true });
  console.log(`\n[record] PASS: recorded the dog-food demo:`);
  console.log(`  - ${outGif} (${(bytes / 1024).toFixed(0)} KiB)`);
}

main().catch((e) => {
  console.error(`[record] FAIL: ${e.message}`);
  process.exit(1);
});
