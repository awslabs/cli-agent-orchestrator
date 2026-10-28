#!/usr/bin/env node
// Local-only end-to-end check of the CAO Dojo page against a real cao-server
// and a real Claude Code agent. Run it through e2e-local.sh, which brings the
// server, the page and the agent up and tears them down. Not wired into CI: it
// needs a logged-in coding CLI on this machine.
//
//   node e2e-local.mjs approve   card appears; Approve writes approved-by-dojo.txt
//   node e2e-local.mjs reject    card appears; Reject leaves no file
//   node e2e-local.mjs none      nothing pending: the run still finishes, no
//                                pending card; an unknown interrupt is RUN_ERROR
//   node e2e-local.mjs page      no server: the page's own state handling, with a
//                                scripted EventSource and a stubbed run
//
// Environment (e2e-local.sh sets these):
//   PAGE_URL        the served index.html (not used by the page phase)
//   CAO_URL         the cao-server under test
//   E2E_WORKDIR     the agent's work dir (approve/reject check the file there)
//   E2E_INTERRUPT_ID  the open interrupt's id (approve/reject): its card must
//                   arrive over the stream before the page posts any run
//   E2E_VIDEO_DIR   opt-in: record a video of the page into this dir
//   E2E_CAPTION     opt-in: caption drawn on the page, e.g. the CAO build
//   E2E_SHOT_DIR    opt-in: save screenshots of each step into this dir
//   E2E_HOLD_MS     opt-in: pause this long on the card and on the result, so a
//                   recording is watchable
import { existsSync, mkdirSync, readFileSync, renameSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

let chromium;
try {
  ({ chromium } = await import("@playwright/test"));
} catch {
  console.error(
    "e2e-local: Playwright is not installed. Run:\n" +
      "  (cd examples/ag-ui/cao-dojo/tools && npm install && npx playwright install chromium)",
  );
  process.exit(2);
}

const phase = process.argv[2];
if (!["approve", "reject", "none", "page"].includes(phase)) {
  console.error("usage: node e2e-local.mjs <approve|reject|none|page>");
  process.exit(2);
}
if (phase === "page") process.exit(await pagePhase());
const PAGE_URL = process.env.PAGE_URL;
const CAO_URL = process.env.CAO_URL;
if (!PAGE_URL || !CAO_URL) {
  console.error(
    "e2e-local: PAGE_URL and CAO_URL must be set (use e2e-local.sh)",
  );
  process.exit(2);
}
const FILE = "approved-by-dojo.txt";
const LINE = "written after approval";
const SETTLE_MS = 15_000;

const t0 = Date.now();
const at = () => `${((Date.now() - t0) / 1000).toFixed(1)}s`;
const failures = [];
const check = (ok, what) => {
  console.log(`${ok ? "PASS" : "FAIL"} ${phase}: ${what}`);
  if (!ok) failures.push(what);
  return ok;
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const hold = () => sleep(Number(process.env.E2E_HOLD_MS ?? 0));

const browser = await chromium.launch();
const videoDir = process.env.E2E_VIDEO_DIR;
if (videoDir) mkdirSync(videoDir, { recursive: true });
const context = await browser.newContext({
  viewport: { width: 1280, height: 800 },
  ...(videoDir
    ? { recordVideo: { dir: videoDir, size: { width: 1280, height: 800 } } }
    : {}),
});
const caption = process.env.E2E_CAPTION;
if (caption) {
  await context.addInitScript((t) => {
    const draw = () => {
      if (document.getElementById("e2e-caption")) return;
      const el = document.createElement("div");
      el.id = "e2e-caption";
      el.textContent = t;
      el.style.cssText =
        "position:fixed;left:0;right:0;bottom:0;z-index:99999;padding:6px 12px;" +
        "font:14px/1.4 monospace;background:#111;color:#fff;pointer-events:none";
      document.body.appendChild(el);
    };
    if (document.body) draw();
    else document.addEventListener("DOMContentLoaded", draw);
  }, `${caption} | ${phase}`);
}
const page = await context.newPage();

const shotDir = process.env.E2E_SHOT_DIR;
if (shotDir) mkdirSync(shotDir, { recursive: true });
async function shot(name) {
  if (!shotDir) return;
  await page
    .screenshot({ path: join(shotDir, `${phase}-${name}.png`) })
    .catch((e) =>
      console.log(`screenshot ${name} failed: ${e.message.split("\n")[0]}`),
    );
}

// Every POST /agui/v1/run the page makes, with how its stream ended.
const runs = [];
page.on("request", (r) => {
  if (r.method() === "POST" && r.url().endsWith("/agui/v1/run")) {
    r._run = {
      start: Date.now(),
      end: null,
      terminal: null,
      outcome: null,
      resume: false,
    };
    r._run.resume = (r.postData() ?? "").includes('"interruptId"');
    runs.push(r._run);
  }
});
page.on("requestfinished", async (r) => {
  if (!r._run) return;
  const body = (await (await r.response())?.text().catch(() => "")) ?? "";
  r._run.terminal = body.includes('"type":"RUN_ERROR"')
    ? "RUN_ERROR"
    : body.includes('"type":"RUN_FINISHED"')
      ? "RUN_FINISHED"
      : null;
  r._run.outcome = body.match(/"outcome":\{"type":"(\w+)"/)?.[1] ?? null;
  r._run.body = body;
  r._run.end = Date.now();
});
page.on("requestfailed", (r) => {
  if (r._run) r._run.end = Date.now();
});

async function agentStatus() {
  const res = await fetch(`${CAO_URL}/sessions/cao-dojo-demo/terminals`);
  const [terminal] = await res.json();
  return (await (await fetch(`${CAO_URL}/terminals/${terminal.id}`)).json())
    .status;
}

// Wait until at least `count` runs were posted and every one of them ended.
async function runsSettled(count) {
  const deadline = Date.now() + SETTLE_MS;
  while (Date.now() < deadline) {
    if (runs.length >= count && runs.every((r) => r.end)) return true;
    await sleep(250);
  }
  return false;
}

const pending = page.locator(
  '[data-testid="approval-card"][data-state="pending"]',
);
const results = page.getByTestId("run-result");

try {
  await page.goto(PAGE_URL);
  await page.locator("#endpoint").fill(CAO_URL);
  await page.locator("#connect").click();
  await page.locator("#status.connected").waitFor({ timeout: SETTLE_MS });

  const prompt = page.locator("#prompt");
  const send = page.locator("#send");

  if (phase === "none") {
    await prompt.fill("Is anything waiting for my approval?");
    await send.click();
    check(await runsSettled(1), `the run ended within ${SETTLE_MS / 1000}s`);
    check(
      runs.length === 1 &&
        runs[0].terminal === "RUN_FINISHED" &&
        runs[0].outcome === "success",
      "RUN_FINISHED with a success outcome",
    );
    await results.first().waitFor({ timeout: SETTLE_MS });
    check(
      (await results.first().getAttribute("data-terminal")) === "RUN_FINISHED",
      "the page shows RUN_FINISHED",
    );
    check((await pending.count()) === 0, "no pending approval card");
    await shot("2-settled");
    await hold();

    // An unknown interrupt must surface as RUN_ERROR, not a hang.
    await page.evaluate(() =>
      window.__caoDojo.resume("00000000-0000-0000-0000-000000000000", {
        approved: true,
      }),
    );
    check(await runsSettled(2), "the unknown-interrupt run ended");
    check(
      runs[1]?.terminal === "RUN_ERROR",
      "an unknown interrupt yields RUN_ERROR",
    );
    await page
      .locator('[data-testid="run-result"][data-terminal="RUN_ERROR"]')
      .first()
      .waitFor({ timeout: SETTLE_MS })
      .then(
        () => check(true, "the page shows RUN_ERROR"),
        () => check(false, "the page shows RUN_ERROR"),
      );
    await shot("3-run-error");
    await hold();
  } else {
    // The card must come from the stream's GENERATIVE_UI frame, so
    // wait for this interrupt's card before the page posts any run. Earlier
    // phases' cards replay too, hence matching on the id e2e-local.sh probed.
    const id = process.env.E2E_INTERRUPT_ID;
    if (!check(Boolean(id), "E2E_INTERRUPT_ID is set"))
      throw new Error("no id");
    const card = page.locator(
      `[data-testid="approval-card"][data-interrupt-id="${id}"]`,
    );
    const streamed = await card
      .and(pending)
      .waitFor({ timeout: SETTLE_MS })
      .then(
        () => true,
        () => false,
      );
    if (!check(streamed, "the approval card arrived over the stream"))
      throw new Error("no card");
    check(runs.length === 0, "before the page posted any run");

    // Then the run plane reports the same interrupt and the page settles any
    // replayed cards, leaving this one as the only pending card.
    await prompt.fill("Is anything waiting for my approval?");
    await send.click();
    check(
      await runsSettled(1),
      `the collecting run ended within ${SETTLE_MS / 1000}s`,
    );
    check(
      runs[0]?.terminal === "RUN_FINISHED" && runs[0]?.outcome === "interrupt",
      "RUN_FINISHED with an interrupt outcome",
    );
    check(
      Boolean(runs[0]?.body.includes(`"id":"${id}"`)),
      "the run lists the same interrupt",
    );
    check(
      (await pending.count()) === 1 && (await card.and(pending).count()) === 1,
      "exactly one pending card, for that interrupt",
    );
    console.log(`${at()} card: ${JSON.stringify(await card.innerText())}`);
    check(
      (await card.getByTestId("approval-reason").innerText()).includes(
        "claude-code:permission_request",
      ),
      "the card shows the reason",
    );
    await shot("1-card");
    await hold();

    const decideAt = runs.length;
    await card
      .getByTestId(phase === "approve" ? "approval-approve" : "approval-reject")
      .click();
    check(
      await runsSettled(decideAt + 1),
      `the resumed run ended within ${SETTLE_MS / 1000}s`,
    );
    const resumed = runs[decideAt];
    check(
      resumed?.resume === true,
      "the decision was posted as a resume[] run",
    );
    check(
      resumed?.terminal === "RUN_FINISHED" && resumed?.outcome === "success",
      "the resumed run finished with a success outcome",
    );
    const decided = phase === "approve" ? "approved" : "rejected";
    check(
      await page
        .locator(
          `[data-testid="approval-card"][data-interrupt-id="${id}"][data-state="${decided}"]`,
        )
        .waitFor({ timeout: 5_000 })
        .then(
          () => true,
          () => false,
        ),
      `the card shows ${decided}`,
    );

    const workdir = process.env.E2E_WORKDIR;
    if (check(Boolean(workdir), "E2E_WORKDIR is set")) {
      const file = join(workdir, FILE);
      if (phase === "approve") {
        let found = false;
        for (let i = 0; i < 120 && !found; i++) {
          found =
            existsSync(file) && readFileSync(file, "utf8").trim() === LINE;
          if (!found) await sleep(500);
        }
        check(found, `${FILE} exists and contains "${LINE}"`);
      } else {
        // Wait for the agent to finish its turn before checking nothing was written.
        let done = false;
        for (let i = 0; i < 120 && !done; i++) {
          done = ["completed", "idle"].includes(
            await agentStatus().catch(() => ""),
          );
          if (!done) await sleep(500);
        }
        check(done, "the agent finished its turn");
        check(!existsSync(file), `${FILE} was not created`);
      }
    }
    await shot("2-settled");
    await hold();
  }
} catch (error) {
  if (!failures.length)
    check(false, `driver error: ${error.message.split("\n")[0]}`);
} finally {
  if (failures.length) await shot("9-failed");
  console.log(
    `${at()} runs: ` +
      JSON.stringify(
        runs.map((r) => ({
          ms: r.end ? r.end - r.start : null,
          terminal: r.terminal,
          outcome: r.outcome,
          resume: r.resume,
        })),
      ),
  );
  const video = page.video();
  await context.close().catch(() => {});
  if (video) {
    await video
      .path()
      .then((p) => renameSync(p, join(videoDir, `${phase}.webm`)))
      .catch(() => {});
  }
  await browser.close().catch(() => {});
}

console.log(
  failures.length ? `FAIL ${phase} (${failures.length})` : `PASS ${phase}`,
);
process.exit(failures.length ? 1 : 0);

// The page phase: no cao-server, no agent. The page is served from a fake
// origin, EventSource is replaced by a scripted one, and POST /agui/v1/run is
// answered with a canned SSE body.
async function pagePhase() {
  const origin = "http://dojo.test";
  const base = "http://cao.test";
  const html = readFileSync(
    join(dirname(fileURLToPath(import.meta.url)), "..", "index.html"),
    "utf8",
  );
  const failed = [];
  const expect = (ok, what) => {
    console.log(`${ok ? "PASS" : "FAIL"} page: ${what}`);
    if (!ok) failed.push(what);
  };
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.addInitScript(() => {
    class FakeEventSource {
      constructor(url) {
        this.url = url;
        this.readyState = 0;
        this.listeners = {};
        window.__es = this;
        (window.__esAll = window.__esAll || []).push(this);
      }
      addEventListener(type, fn) {
        (this.listeners[type] = this.listeners[type] || []).push(fn);
      }
      close() {
        this.readyState = 2;
      }
      open() {
        this.readyState = 1;
        if (this.onopen) this.onopen({});
      }
      // Like a real EventSource, a frame with no id: line reports the last id
      // seen on this object, across events and reconnects.
      emit(type, data, id) {
        if (id) this.lastEventId = id;
        const ev = { data: typeof data === "string" ? data : JSON.stringify(data), lastEventId: this.lastEventId || "" };
        (this.listeners[type] || []).forEach((fn) => fn(ev));
      }
      fail(readyState) {
        this.readyState = readyState;
        if (this.onerror) this.onerror({});
      }
    }
    FakeEventSource.CONNECTING = 0;
    FakeEventSource.OPEN = 1;
    FakeEventSource.CLOSED = 2;
    window.EventSource = FakeEventSource;
  });
  await page.route(`${origin}/**`, (route) =>
    route.fulfill({ contentType: "text/html", body: html }),
  );
  const finished = (outcome) =>
    `data: ${JSON.stringify({ type: "RUN_STARTED" })}\n\n` +
    `data: ${JSON.stringify({ type: "RUN_FINISHED", outcome })}\n\n`;
  let runBody = finished({ type: "success" });
  // When set, the stubbed run response waits for this promise, so a test can
  // push stream frames while a run is still in flight.
  let holdRun = null;
  const cors = {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Headers": "Content-Type, Accept, Authorization",
    "Access-Control-Allow-Methods": "POST",
  };
  await page.route("http://*.test/agui/v1/run", async (route) => {
    if (route.request().method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
    if (holdRun) await holdRun;
    return route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      headers: cors,
      body: runBody,
    });
  });
  const card = (id) => ({
    component: "approval_card",
    props: { interrupt_id: id, reason: "claude-code:permission_request", options: ["approve", "reject"] },
  });
  const stateOf = (id) =>
    page
      .locator(`[data-testid="approval-card"][data-interrupt-id="${id}"]`)
      .getAttribute("data-state", { timeout: 2_000 })
      .catch(() => null);
  const connect = async (url = base) => {
    if (await page.locator("#connect").isDisabled()) await page.locator("#disconnect").click();
    await page.locator("#endpoint").fill(url);
    await page.locator("#connect").click();
    await page.evaluate(() => window.__es.open());
  };
  const emit = (type, data, id) =>
    page.evaluate(([t, d, i]) => window.__es.emit(t, d, i), [type, data, id]);

  try {
    await page.goto(`${origin}/index.html`);
    await connect();

    // 1. The 40-card cap never prunes a pending card.
    await emit("GENERATIVE_UI", card("keep-me"), "e1");
    for (let i = 0; i < 45; i++) {
      await emit("GENERATIVE_UI", { component: "metric", props: { label: "m" + i, value: i } }, "m" + i);
    }
    expect((await stateOf("keep-me")) === "pending", "a pending card survives the 40-item cap");

    // 2. Deciding with no base URL puts the card back to pending.
    await page.locator("#endpoint").fill("");
    await page
      .locator('[data-interrupt-id="keep-me"] [data-testid="approval-approve"]')
      .click();
    expect((await stateOf("keep-me")) === "pending", "an empty base URL leaves the card pending, not sending");
    expect(
      await page
        .locator('[data-interrupt-id="keep-me"] [data-testid="approval-approve"]')
        .isEnabled(),
      "its buttons are enabled again",
    );

    // 3. A resolved card stays resolved when a reconnect replays its frame.
    await connect();
    await emit("GENERATIVE_UI", card("done-1"), "d1");
    runBody = finished({ type: "success" });
    await page
      .locator('[data-interrupt-id="done-1"] [data-testid="approval-approve"]')
      .click();
    expect(
      await page
        .locator('[data-interrupt-id="done-1"][data-state="approved"]')
        .waitFor({ timeout: 5_000 })
        .then(() => true, () => false),
      "a stubbed RUN_FINISHED marks the card approved",
    );
    await connect();
    await emit("GENERATIVE_UI", card("done-1"), "d1");
    await emit("GENERATIVE_UI", card("done-1"), "d1-again");
    const replayed = await stateOf("done-1");
    expect(replayed !== null && replayed !== "pending", `a replayed resolved card is not pending (got ${replayed})`);

    // 4. A STATE_DELTA with one bad op changes nothing.
    await emit("STATE_SNAPSHOT", { snapshot: { sessions: [{ name: "s1", status: "idle" }], terminals: [] } }, "s1");
    await emit("STATE_DELTA", {
      delta: [
        { op: "replace", path: "/sessions/0/status", value: "processing" },
        { op: "remove", path: "/nope/missing" },
      ],
    }, "s2");
    expect(
      (await page.locator("#fleet .fleet-session .chip").first().innerText()) === "idle",
      "a patch with a failing op leaves the fleet state unchanged",
    );

    // 5. Switching servers clears the old server's fleet and cards.
    await connect("http://other.test");
    expect((await page.locator("#fleet .fleet-session").count()) === 0, "connect() clears the fleet panel");
    expect((await page.locator('[data-testid="approval-card"]').count()) === 0, "connect() clears the cards");

    // 6. A non-finite progress value never renders "NaN%" or "Infinity%".
    await emit("GENERATIVE_UI", '{"component":"progress","props":{"label":"p","value":1e999}}', "p1");
    const progress = await page.locator("#components").innerText();
    expect(!/NaN%|Infinity%/.test(progress), "a non-finite progress value is not shown as a percentage");

    // 7. A transient error after the stream opened says reconnecting.
    await emit("STATE_SNAPSHOT", { snapshot: { sessions: [], terminals: [] } }, "x1");
    await page.evaluate(() => window.__es.fail(0));
    const status = await page.locator("#statusText").innerText();
    expect(/reconnecting/i.test(status) && !/no CAO server/.test(status), `a transient error shows reconnecting (got "${status}")`);

    // 8. STATE_SNAPSHOT and STATE_DELTA carry no id: line, so they report the
    // previous record's id. They must never be deduped; records still are.
    await connect();
    const record = (n) => ({ component: "metric", props: { label: "rec-" + n, value: n } });
    for (const n of [1, 2, 3]) {
      if (n > 1) await page.evaluate(() => { window.__es.fail(0); window.__es.open(); });
      await emit("GENERATIVE_UI", record(n), "evt-" + n);
      await emit("STATE_SNAPSHOT", { snapshot: { sessions: [{ name: "conn-" + n, status: "idle" }], terminals: [] } });
      await emit("STATE_DELTA", { delta: [{ op: "add", path: "/sessions/-", value: { name: "delta-" + n, status: "idle" } }] });
      if (n === 3) await emit("GENERATIVE_UI", record(2), "evt-2");
    }
    const fleet = await page.locator("#fleet .sname").allInnerTexts();
    expect(
      JSON.stringify(fleet) === JSON.stringify(["conn-3", "delta-3"]),
      `the fleet shows the last reconnect's snapshot plus its delta (got ${JSON.stringify(fleet)})`,
    );
    const rec2 = await page.locator("#components .gtitle", { hasText: /^rec-2$/ }).count();
    expect(rec2 === 1, `a re-sent record is still dropped (rec-2 cards: ${rec2})`);

    // 9. The server reads its pending list once per run. A card that arrives
    // while the run is in flight is newer than that list and must stay pending;
    // a card that was open when the run started and is not listed is settled.
    await connect();
    await emit("GENERATIVE_UI", card("early"), "c-early");
    let release;
    holdRun = new Promise((r) => { release = r; });
    runBody = finished({ type: "success" });
    await page.locator("#send").click();
    await page.waitForTimeout(200);
    await emit("GENERATIVE_UI", card("late"), "c-late");
    release();
    holdRun = null;
    await page
      .locator('[data-testid="run-result"]')
      .first()
      .waitFor({ timeout: 5_000 })
      .catch(() => {});
    expect((await stateOf("early")) === "settled", `a card open before the run and not listed is settled (got ${await stateOf("early")})`);
    expect((await stateOf("late")) === "pending", `a card that arrived during the run stays pending (got ${await stateOf("late")})`);
    expect(
      await page.locator('[data-interrupt-id="late"] [data-testid="approval-approve"]').isEnabled(),
      "the late card's Approve stays enabled",
    );

    // 10. Session-end closer frames have no id: line either. After a record
    // they report its id, and must still reach the log.
    await connect();
    await emit("GENERATIVE_UI", record(10), "evt-10");
    await emit("TOOL_CALL_END", { toolCallId: "t1", message: "closer-one" });
    await emit("TOOL_CALL_END", { toolCallId: "t2", message: "closer-two" });
    const logText = await page.locator("#log").innerText();
    expect(
      /closer-one/.test(logText) && /closer-two/.test(logText),
      "frames without an id: line after a record are logged, not dropped",
    );
  } catch (error) {
    expect(false, `driver error: ${error.message.split("\n")[0]}`);
  } finally {
    await browser.close().catch(() => {});
  }
  console.log(failed.length ? `FAIL page (${failed.length})` : "PASS page");
  return failed.length ? 1 : 0;
}
