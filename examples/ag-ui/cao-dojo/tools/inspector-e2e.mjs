#!/usr/bin/env node
// Keyless check of the CAO Dojo raw event inspector (#1165). It serves the
// cao-dojo directory from a loopback static server (no cao-server, no route),
// opens inspector.html on a recorded trace, and asserts that the page shows
// EVERY record of the trace, in file order, with its type and its timestamp.
//
//   node inspector-e2e.mjs [trace-path-relative-to-cao-dojo]
//
// The expected rows are computed here from the .ndjson itself, never from the
// page, so the page cannot agree with itself. A positive looks like
// "inspector-e2e: PASS rows=22 ..."; any mismatch prints the first difference
// and exits 1.
//
// Environment:
//   E2E_SHOT_DIR  opt-in: write inspector.png (full page) into this dir
import { createServer } from "node:http";
import { existsSync, mkdirSync, readFileSync } from "node:fs";
import { dirname, extname, join, normalize, sep } from "node:path";
import { fileURLToPath } from "node:url";

let chromium;
try {
  ({ chromium } = await import("@playwright/test"));
} catch {
  console.error(
    "inspector-e2e: Playwright is not installed. Run:\n" +
      "  (cd examples/ag-ui/cao-dojo/tools && npm ci && npx playwright install chromium)",
  );
  process.exit(2);
}

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const tracePath = process.argv[2] || "trace/cao-dojo-trace.ndjson";
const shotDir = process.env.E2E_SHOT_DIR;
const TYPES = { ".html": "text/html", ".ndjson": "application/x-ndjson", ".json": "application/json" };

// What the page must show, from the file alone. The first line is `_meta`, the
// recorder's header; it is not an event and is not a row.
function expectedRows(path) {
  const rows = [];
  for (const line of readFileSync(join(ROOT, path), "utf8").split("\n")) {
    if (!line.trim()) continue;
    const rec = JSON.parse(line);
    if ("_meta" in rec) continue;
    const frame = rec.frame;
    const ts = frame && Number.isInteger(frame.timestamp) ? String(frame.timestamp) : "";
    rows.push({
      type: frame ? frame.type : "REQUEST",
      timestamp: ts,
      iso: ts ? new Date(Number(ts)).toISOString() : "",
      dir: rec.dir,
      plane: rec.plane,
      capture: rec.capture,
      seq: String(rec.seq),
    });
  }
  return rows;
}

function fail(msg) {
  console.error(`inspector-e2e: FAIL ${msg}`);
  process.exitCode = 1;
}

const server = createServer((req, res) => {
  const rel = normalize(decodeURIComponent(new URL(req.url, "http://x").pathname)).replace(/^[/\\]+/, "");
  const file = join(ROOT, rel);
  if (!file.startsWith(ROOT + sep) || !existsSync(file)) {
    res.writeHead(404).end("not found");
    return;
  }
  res.writeHead(200, { "content-type": TYPES[extname(file)] || "application/octet-stream" });
  res.end(readFileSync(file));
});
await new Promise((ok) => server.listen(0, "127.0.0.1", ok));
const base = `http://127.0.0.1:${server.address().port}`;

let browser;
try {
  const want = expectedRows(tracePath);
  browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const requests = [];
  page.on("request", (r) => requests.push(`${r.method()} ${new URL(r.url()).pathname}`));
  const resp = await page.goto(`${base}/inspector.html?trace=${encodeURIComponent(tracePath)}`);
  if (!resp || resp.status() !== 200) {
    fail(`inspector.html did not load (status ${resp && resp.status()})`);
  } else {
    await page.waitForSelector("#events tbody tr", { timeout: 10_000 });
    const got = await page.$$eval("#events tbody tr", (trs) =>
      trs.map((tr) => ({
        type: tr.dataset.type,
        timestamp: tr.dataset.timestamp,
        iso: tr.querySelector(".c-iso").textContent.trim(),
        dir: tr.dataset.dir,
        plane: tr.dataset.plane,
        capture: tr.dataset.capture,
        seq: tr.dataset.seq,
        shownType: tr.querySelector(".c-type").textContent.trim(),
      })),
    );
    if (got.length !== want.length) {
      fail(`page shows ${got.length} rows, trace has ${want.length} records`);
    } else {
      for (let i = 0; i < want.length && !process.exitCode; i++) {
        for (const k of Object.keys(want[i])) {
          const shown = k === "iso" && !want[i].iso ? "—" : want[i][k];
          const have = k === "iso" ? got[i].iso : got[i][k];
          if (have !== shown) fail(`row ${i + 1} ${k}: page "${have}", trace "${shown}"`);
        }
        if (!process.exitCode && got[i].shownType !== want[i].type) {
          fail(`row ${i + 1} visible type: page "${got[i].shownType}", trace "${want[i].type}"`);
        }
      }
    }
    // Read-only: the page may fetch the trace and nothing else, and never write.
    const stray = requests.filter((r) => r !== `GET /${tracePath}` && r !== "GET /inspector.html" && !r.startsWith("GET /favicon"));
    if (stray.length) fail(`unexpected network requests: ${stray.join(", ")}`);
    if (shotDir) {
      mkdirSync(shotDir, { recursive: true });
      await page.screenshot({ path: join(shotDir, "inspector.png"), fullPage: true });
    }
    if (!process.exitCode) {
      const withTs = want.filter((r) => r.timestamp).length;
      console.log(`inspector-e2e: PASS rows=${want.length} with-timestamp=${withTs} types=${[...new Set(want.map((r) => r.type))].join(",")}`);
    }
  }
} catch (e) {
  fail(String(e && e.message ? e.message.split("\n")[0] : e));
} finally {
  if (browser) await browser.close();
  server.close();
}
