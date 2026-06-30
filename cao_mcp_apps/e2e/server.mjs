#!/usr/bin/env node
// E2E harness server for the CAO MCP App Playwright tier.
//
// Serves three things on a single origin so the View bundles run exactly as the
// host would load them (same-origin iframe + same-origin SSE):
//
//   GET  /host.html?view=<dashboard|agent|event-stream|combo>
//        -> the MCP host harness page (host.html). It embeds the matching built
//           single-file bundle(s) in an iframe and runs the host JSON-RPC peer
//           (host.js) that answers ui/initialize, tools/call, etc.
//   GET  /bundles/<view>.html
//        -> the built single-file bundle from ext_apps/apps_static.
//   GET  /host.js                     -> the in-browser host peer.
//   GET  /events                      -> a text/event-stream SSE feed.
//   POST /emit            {event}     -> push one SSE frame to every subscriber.
//
// No real Backplane is required: the host peer answers all read tools from
// canned, mutable fleet state, and a submit_command that emits a governance
// event is relayed to /emit so the live event-stream view updates.
//
// The default port is 9889 so the event-stream bundle's default
// `backplaneBaseUrl` (http://127.0.0.1:9889) resolves to this same origin.

import { createReadStream, existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { createServer } from "node:http";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.E2E_PORT ?? 9889);
const BUNDLES_DIR = resolve(
  __dirname,
  "../../src/cli_agent_orchestrator/ext_apps/apps_static",
);

/** Active SSE subscribers (the event-stream iframe connects here). */
const sseClients = new Set();

function sendFile(res, path, type) {
  if (!existsSync(path)) {
    res.writeHead(404, { "content-type": "text/plain" });
    res.end(`not found: ${path}`);
    return;
  }
  res.writeHead(200, { "content-type": type, "cache-control": "no-store" });
  createReadStream(path).pipe(res);
}

async function handle(req, res) {
  const url = new URL(req.url, `http://127.0.0.1:${PORT}`);
  const path = url.pathname;

  if (path === "/" || path === "/host.html") {
    sendFile(res, resolve(__dirname, "host.html"), "text/html; charset=utf-8");
    return;
  }
  if (path === "/host.js") {
    sendFile(
      res,
      resolve(__dirname, "host.js"),
      "text/javascript; charset=utf-8",
    );
    return;
  }
  if (path.startsWith("/bundles/")) {
    const name = path.slice("/bundles/".length);
    if (!/^[a-z-]+\.html$/.test(name)) {
      res.writeHead(400).end("bad bundle name");
      return;
    }
    sendFile(res, resolve(BUNDLES_DIR, name), "text/html; charset=utf-8");
    return;
  }
  if (path === "/events") {
    res.writeHead(200, {
      "content-type": "text/event-stream",
      "cache-control": "no-cache",
      connection: "keep-alive",
      "access-control-allow-origin": "*",
    });
    res.write(": connected\n\n");
    sseClients.add(res);
    req.on("close", () => sseClients.delete(res));
    return;
  }
  if (path === "/emit" && req.method === "POST") {
    let body = "";
    for await (const chunk of req) body += chunk;
    let event;
    try {
      event = JSON.parse(body || "{}").event;
    } catch {
      res.writeHead(400).end("bad json");
      return;
    }
    const frame = `data: ${JSON.stringify(event)}\n\n`;
    for (const client of sseClients) client.write(frame);
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ delivered: sseClients.size }));
    return;
  }

  res.writeHead(404, { "content-type": "text/plain" });
  res.end("not found");
}

createServer((req, res) => {
  handle(req, res).catch((err) => {
    res.writeHead(500, { "content-type": "text/plain" });
    res.end(String(err));
  });
}).listen(PORT, "127.0.0.1", () => {
  // eslint-disable-next-line no-console
  console.log(
    `[e2e] harness server on http://127.0.0.1:${PORT} (bundles: ${BUNDLES_DIR})`,
  );
});
