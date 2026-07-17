#!/usr/bin/env node
// Shift-left demo recorder for the dependency-free AG-UI EventSource viewer.
//
// This is BUILD/CI tooling, not part of the shipped viewer (../index.html has
// zero dependencies). It boots a CAO_AGUI_ENABLED cao-server + a static server
// for the viewer, drives Chromium through the live generative-UI flow while
// recording video, ASSERTS that every allow-listed component renders and that
// the off-list component is refused (these assertions are the shift-left test —
// a non-zero exit fails the build if the stream->component contract drifts),
// and exports an optimized GIF for the PR description + docs when a gif-capable
// ffmpeg is available (Playwright's bundled ffmpeg is webm-only).
//
// Artifacts (docs/media/): agui-eventsource-viewer-demo.webm and
// agui-eventsource-viewer-demo.gif (when a gif-capable ffmpeg is found).
//
// Usage:  npm ci && npm run playwright:install && npm run record
// Env:    CAO_AGUI_BASE (default http://localhost:9889), STATIC_PORT (8123),
//         FFMPEG_BIN (default: ffmpeg on PATH), CAO_REPO (repo root).

import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readdirSync, renameSync, rmSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "@playwright/test";

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO = process.env.CAO_REPO || resolve(__dirname, "..", "..", "..");
const AGUI_PORT = Number(process.env.AGUI_PORT ?? 9889);
const STATIC_PORT = Number(process.env.STATIC_PORT ?? 8123);
const BASE = process.env.CAO_AGUI_BASE || `http://localhost:${AGUI_PORT}`;
const VIEWER_URL = `http://localhost:${STATIC_PORT}/examples/agui-eventsource-viewer/`;
const OUT_DIR = resolve(REPO, "docs/media");
const TMP_DIR = resolve(__dirname, ".demo-tmp");
const FFMPEG_BIN = process.env.FFMPEG_BIN || "ffmpeg";
const VIEWPORT = { width: 1280, height: 800 };

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// The six allow-listed components + representative props (mirrors showcase.sh).
const COMPONENTS = [
  ["agent_card", { name: "worker-1", provider: "kiro_cli", status: "working" }],
  ["metric", { label: "tokens", value: 12840, unit: "tok" }],
  ["progress", { label: "Indexing repo", value: 0.42 }],
  ["diff_summary", { title: "Refactor rpc handler", files: [{ path: "a2a/rpc.py", additions: 74, deletions: 3 }] }],
  ["choice_prompt", { question: "Pick a branch", choices: [{ label: "main", value: "main" }, { label: "release", value: "release" }] }],
  ["approval_card", { title: "Deploy to prod?", detail: "3 files, 1 migration", risk: "high" }],
];

async function waitFor(url, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const r = await fetch(url);
      if (r.status < 500) return;
    } catch {
      /* not up yet */
    }
    await sleep(300);
  }
  throw new Error(`server did not become ready: ${url}`);
}

function ffmpegHasGif(bin) {
  try {
    const r = spawnSync(bin, ["-hide_banner", "-encoders"], { encoding: "utf8" });
    return r.status === 0 && /\bgif\b/.test(r.stdout || "");
  } catch {
    return false;
  }
}

function run(bin, args) {
  return new Promise((res, rej) => {
    const p = spawn(bin, args, { stdio: "inherit" });
    p.on("exit", (code) => (code === 0 ? res() : rej(new Error(`${bin} exited ${code}`))));
    p.on("error", rej);
  });
}

// Read an SSE response body until `predicate(accumulatedText)` is true or the
// timeout elapses, then abort. Returns the accumulated text. Used by the F1b
// reconnect proof to inspect raw AG-UI frames end-to-end.
async function readSseUntil(url, { headers = {}, predicate, timeoutMs = 8000 } = {}) {
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), timeoutMs);
  let text = "";
  try {
    const resp = await fetch(url, { headers, signal: ac.signal });
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const { value, done } = await reader.read();
      if (done) break;
      text += dec.decode(value, { stream: true });
      if (predicate && predicate(text)) break;
    }
  } catch (e) {
    if (e.name !== "AbortError") throw e;
  } finally {
    clearTimeout(timer);
    ac.abort();
  }
  return text;
}

// F1b (shift-left e2e): prove that a client which drops mid-stream and
// reconnects with `Last-Event-ID` gets the event it missed replayed EXACTLY
// once — no gap, no duplicate. Exercises the real endpoint + EventLog.after_id
// + the overflow-close subscription path end-to-end against the live server.
async function proveReconnectNoGap(base, emit) {
  const streamUrl = `${base}/agui/v1/stream`;

  // 1) Connect reader #1 and wait until it is registered (STATE_SNAPSHOT seen),
  //    then emit an event and capture the SSE `id:` cursor it carries.
  const ac1 = new AbortController();
  const resp1 = await fetch(streamUrl, { signal: ac1.signal });
  const reader1 = resp1.body.getReader();
  const dec1 = new TextDecoder();
  let buf1 = "";

  const readMore1 = async () => {
    const { value, done } = await reader1.read();
    if (!done && value) buf1 += dec1.decode(value, { stream: true });
    return !done;
  };

  let dl = Date.now() + 8000;
  while (Date.now() < dl && !buf1.includes("STATE_SNAPSHOT")) {
    if (!(await readMore1())) break;
  }

  await emit("metric", { label: "recon-B", value: 1 });

  let idB = null;
  dl = Date.now() + 8000;
  while (Date.now() < dl && !idB) {
    if (buf1.includes("recon-B")) {
      for (const frame of buf1.split("\n\n")) {
        if (frame.includes("recon-B")) {
          const m = frame.match(/(?:^|\n)id:\s*([^\n]+)/);
          if (m) idB = m[1].trim();
        }
      }
      if (idB) break;
    }
    if (!(await readMore1())) break;
  }
  ac1.abort(); // drop the connection (simulate a transport interruption)
  if (!idB) throw new Error("F1b: did not capture the id of the live 'recon-B' event");

  // 2) While disconnected, emit the event the client will MISS.
  await emit("metric", { label: "recon-C-missed", value: 2 });

  // 3) Reconnect with Last-Event-ID — the missed event is replayed exactly once,
  //    and the already-seen event is NOT replayed (strictly-after contract).
  const replay = await readSseUntil(streamUrl, {
    headers: { "Last-Event-ID": idB },
    predicate: (t) => t.includes("recon-C-missed"),
    timeoutMs: 8000,
  });
  const missed = (replay.match(/recon-C-missed/g) || []).length;
  if (missed !== 1) throw new Error(`F1b: missed event replayed ${missed}x (expected exactly 1)`);
  if (replay.includes("recon-B")) {
    throw new Error("F1b: an already-delivered event was replayed (no-dup contract broken)");
  }
  console.log("[demo] PASS(F1b): reconnect (Last-Event-ID) replayed the missed event exactly once.");
}

async function main() {
  rmSync(TMP_DIR, { recursive: true, force: true });
  mkdirSync(TMP_DIR, { recursive: true });
  mkdirSync(OUT_DIR, { recursive: true });

  // Boot cao-server with the AG-UI surface only if one isn't already serving
  // (avoids a port clash when a server is already up, e.g. under orchestration).
  let server = null;
  const alreadyUp = await fetch(`${BASE}/health`).then((r) => r.ok).catch(() => false);
  if (!alreadyUp) {
    server = spawn("uv", ["run", "cao-server"], {
      cwd: REPO,
      env: {
        ...process.env,
        CAO_AGUI_ENABLED: "1",
        CAO_CORS_ORIGINS: `http://localhost:${STATIC_PORT}`,
      },
      stdio: "inherit",
    });
  } else {
    console.log(`[demo] reusing cao-server already reachable at ${BASE}`);
  }
  const staticSrv = spawn("python3", ["-m", "http.server", String(STATIC_PORT)], {
    cwd: REPO,
    stdio: "inherit",
  });

  let failure = null;
  try {
    await waitFor(`${BASE}/health`);
    await waitFor(VIEWER_URL);

    const browser = await chromium.launch({
      headless: true,
      args: ["--no-sandbox"],
      // Reuse a specific browser binary when provided (CI supplies one; also
      // lets a dev machine skip the version-pinned download). Falls back to the
      // Playwright-managed browser for this @playwright/test version.
      ...(process.env.CHROMIUM_BIN ? { executablePath: process.env.CHROMIUM_BIN } : {}),
    });
    const context = await browser.newContext({
      viewport: VIEWPORT,
      recordVideo: { dir: TMP_DIR, size: VIEWPORT },
    });
    const page = await context.newPage();
    await page.goto(VIEWER_URL, { waitUntil: "domcontentloaded" });

    // Wait for the connect banner (STATE_SNAPSHOT hydration).
    await page.getByText("connected", { exact: false }).first().waitFor({ timeout: 15000 });
    await sleep(1200);

    // ── F3 (shift-left): the viewer builds the stream URL with the access token
    // as a `?access_token=` query param (native EventSource can't set headers),
    // holds it in memory only, and never persists it. Deterministic, no auth
    // server needed — assert the exposed URL builder directly in the browser.
    const urlNoToken = await page.evaluate((b) => window.__caoBuildStreamUrl(b, ""), BASE);
    const urlWithToken = await page.evaluate(
      (b) => window.__caoBuildStreamUrl(b, "jwt-abc.def.ghi"),
      BASE,
    );
    if (urlNoToken !== `${BASE}/agui/v1/stream`) {
      throw new Error(`F3: no-token URL should have no query, got: ${urlNoToken}`);
    }
    if (!/[?&]access_token=jwt-abc\.def\.ghi(?:&|$)/.test(urlWithToken)) {
      throw new Error(`F3: token not attached as a query param, got: ${urlWithToken}`);
    }
    const leaked = await page.evaluate(() => {
      function dump(s) {
        let o = "";
        for (let i = 0; i < s.length; i++) o += s.key(i) + "=" + s.getItem(s.key(i)) + ";";
        return o;
      }
      return (dump(localStorage) + dump(sessionStorage)).indexOf("jwt-abc") >= 0;
    });
    if (leaked) throw new Error("F3: access token leaked into web storage (must be in-memory only)");
    console.log("[demo] PASS(F3): stream URL carries access_token via searchParams; not persisted.");

    const emit = (component, props) =>
      context.request.post(`${BASE}/agui/v1/emit_ui`, { data: { component, props } });

    // Drive + ASSERT each allow-listed component renders (shift-left gate).
    for (const [component, props] of COMPONENTS) {
      const resp = await emit(component, props);
      if (resp.status() !== 200) throw new Error(`emit ${component} -> HTTP ${resp.status()} (expected 200)`);
      // The component card shows its type label; assert it appears in the UI.
      await page
        .locator("#components .gcard .gtype", { hasText: component })
        .first()
        .waitFor({ timeout: 8000 });
      await sleep(650);
    }

    // Off-list component: the server MUST refuse it (400) and it MUST NOT render.
    const offResp = await emit("iframe", { src: "https://evil.example" });
    if (offResp.status() !== 400) throw new Error(`off-list iframe -> HTTP ${offResp.status()} (expected 400 refusal)`);
    await sleep(800);
    const iframeCount = await page.locator("#components iframe").count();
    if (iframeCount !== 0) throw new Error(`off-list component rendered a live <iframe> (${iframeCount}) — safety contract broken`);

    // Final assertion: all six components are on screen.
    const rendered = await page.locator("#components .gcard").count();
    if (rendered < COMPONENTS.length) throw new Error(`only ${rendered}/${COMPONENTS.length} components rendered`);
    await sleep(1500);

    // ── F1b (shift-left e2e): reconnect via Last-Event-ID replays a missed event
    // exactly once (no gap, no dup) — the reviewer's reconnect repro, end-to-end.
    await proveReconnectNoGap(BASE, emit);

    await context.close(); // finalizes the .webm
    await browser.close();

    const webm = readdirSync(TMP_DIR).find((f) => f.endsWith(".webm"));
    if (!webm) throw new Error("no video captured");
    const outWebm = resolve(OUT_DIR, "agui-eventsource-viewer-demo.webm");
    renameSync(resolve(TMP_DIR, webm), outWebm);
    console.log(`[demo] wrote ${outWebm}`);

    // Optimized GIF for the PR/docs when a gif-capable ffmpeg exists.
    const outGif = resolve(OUT_DIR, "agui-eventsource-viewer-demo.gif");
    if (ffmpegHasGif(FFMPEG_BIN)) {
      const palette = resolve(TMP_DIR, "palette.png");
      const vf = "fps=10,scale=800:-1:flags=lanczos";
      await run(FFMPEG_BIN, ["-y", "-i", outWebm, "-vf", `${vf},palettegen`, palette]);
      await run(FFMPEG_BIN, ["-y", "-i", outWebm, "-i", palette, "-lavfi", `${vf} [x]; [x][1:v] paletteuse`, outGif]);
      console.log(`[demo] wrote ${outGif}`);
    } else {
      console.log(`[demo] gif-capable ffmpeg not found (${FFMPEG_BIN}); kept ${outWebm} only.`);
    }
    console.log("[demo] PASS: 6 components rendered live, off-list refused, GIF exported.");
  } catch (err) {
    failure = err;
  } finally {
    if (server) server.kill("SIGTERM");
    staticSrv.kill("SIGTERM");
  }
  if (failure) {
    console.error(`[demo] FAIL: ${failure.message}`);
    process.exit(1);
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
