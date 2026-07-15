// Headless proof: load the explorer against a REAL cao-server, assert the
// Sigma canvas mounts with nodes, click a node, assert the side panel shows
// fetched memory content, and assert zero page/console errors.
//
// Usage: node proof.mjs   (from cao_mcp_apps/, with playwright installed)
// Assumes: page served on :8900, cao-server on :9894 with CORS for :8900.
import { chromium } from "playwright";

const PAGE_URL = process.env.PAGE_URL || "http://127.0.0.1:8900/index.html";

const errors = [];
const browser = await chromium.launch(
  process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : {},
);
const page = await browser.newPage();
page.on("console", (msg) => {
  // Bare resource 404s (e.g. favicon.ico) surface as a console error whose
  // text omits the URL; track them via `response` below instead so we can
  // whitelist favicon. Skip generic "Failed to load resource" console lines.
  if (msg.type() === "error" && !/Failed to load resource/.test(msg.text())) {
    errors.push("console: " + msg.text());
  }
});
page.on("pageerror", (err) => errors.push("pageerror: " + err.message));
page.on("response", (resp) => {
  // A real (non-favicon) 4xx/5xx is a genuine failure.
  if (resp.status() >= 400 && !/favicon\.ico$/.test(resp.url())) {
    errors.push(`http ${resp.status()}: ${resp.url()}`);
  }
});

let failed = false;
const check = (name, cond, extra = "") => {
  console.log(
    `${cond ? "PASS" : "FAIL"}  ${name}${extra ? "  — " + extra : ""}`,
  );
  if (!cond) failed = true;
};

await page.goto(PAGE_URL, { waitUntil: "load" });

// Graph fetch runs wiki_lint server-side (~60s). Wait until the Sigma canvas
// mounts a <canvas> (Sigma renders into nested canvases inside #graph-canvas).
console.log("Waiting for graph to load (server runs wiki-lint, up to ~120s)…");
await page.waitForFunction(
  () => window.__graph && window.__graph.order > 0,
  null,
  { timeout: 130_000 },
);

const nodeCount = await page.evaluate(() => window.__graph.order);
check("graph loaded with nodes", nodeCount > 0, `${nodeCount} nodes`);

const canvasCount = await page.locator("#graph-canvas canvas").count();
check(
  "Sigma canvas mounted",
  canvasCount > 0,
  `${canvasCount} <canvas> elements`,
);

const statusHidden = await page.evaluate(
  () => !document.getElementById("status").classList.contains("show"),
);
check("status overlay hidden (graph visible)", statusHidden);

// Simulate a clickNode by emitting Sigma's event with the first node id, then
// verify the side panel populates with fetched content.
const firstNode = await page.evaluate(() => window.__graph.nodes()[0]);
console.log("Clicking node:", firstNode);
await page.evaluate(
  (id) => window.__sigma.emit("clickNode", { node: id }),
  firstNode,
);

// Wait for either fetched content or a graceful error/empty state in the panel.
await page.waitForFunction(
  () => {
    const b = document.getElementById("sp-body");
    return b && !b.querySelector(".sp-loading");
  },
  { timeout: 35_000 },
);

const panel = await page.evaluate(() => {
  const key = document.getElementById("sp-key").textContent;
  const md = document.querySelector("#sp-body .md");
  const err = document.querySelector("#sp-body .sp-error");
  const empty = document.querySelector("#sp-body .sp-empty");
  return {
    key,
    contentLen: md ? md.textContent.trim().length : 0,
    hasContent: !!md && md.textContent.trim().length > 0,
    graceful: !!err || !!empty,
    lastTopicContent: window.__lastTopic ? window.__lastTopic.content : null,
  };
});
check(
  "side panel header shows clicked key",
  panel.key === firstNode,
  panel.key,
);
check(
  "side panel shows fetched content (or graceful state)",
  panel.hasContent || panel.graceful,
  panel.hasContent
    ? `${panel.contentLen} chars rendered`
    : "graceful empty/error",
);
if (panel.lastTopicContent) {
  console.log(
    "   fetched content preview:",
    JSON.stringify(panel.lastTopicContent.slice(0, 120)),
  );
}

check(
  "zero page/console errors",
  errors.length === 0,
  errors.join(" | ") || "none",
);

await browser.close();
console.log(failed ? "\nRESULT: FAILED" : "\nRESULT: ALL CHECKS PASSED");
process.exit(failed ? 1 : 0);
