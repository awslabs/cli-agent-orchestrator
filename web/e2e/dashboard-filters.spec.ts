import { test, expect, Page, Locator } from "@playwright/test";
import { AxeBuilder } from "@axe-core/playwright";
import type { Annotation, AnnotationsResponse } from "../src/api";
import { projectedTerminal } from "../src/test/projectedTerminal";
import { stubBackend } from "./stub";

// Filter-bar e2e evidence, at 1280×800 and 390×844 (both configured
// projects). The stub serves a REAL fleet for these specs — several sessions,
// several profiles, several statuses, and annotations carrying one of every
// control shape — because the default one-terminal stub makes every filter
// assertion vacuous.

const SHOTS = "e2e/__screenshots__/filters";

const SESSIONS = [
  { id: "s-1", name: "cao-alpha", status: "active" },
  { id: "s-2", name: "cao-beta", status: "active" },
  { id: "s-3", name: "cao-empty", status: "active" },
];

const ALPHA = [
  projectedTerminal({
    id: "aa-0001",
    agent_profile: "implementer",
    provider: "kimi_cli",
    status: "not_fifo_monitored",
    caller_id: null,
    last_active: "2026-07-28T12:00:00Z",
  }),
  projectedTerminal({
    id: "aa-0002",
    agent_profile: "implementer",
    provider: "kimi_cli",
    status: "not_fifo_monitored",
    caller_id: "aa-0001",
    last_active: "2026-07-28T11:00:00Z",
  }),
  projectedTerminal({
    id: "aa-0003",
    agent_profile: null,
    provider: "claude_code",
    status: "idle",
    last_active: "2026-07-28T10:00:00Z",
  }),
  projectedTerminal({
    id: "aa-0004",
    agent_profile: "reviewer",
    provider: "claude_code",
    status: "stopped",
    last_active: "2026-07-28T09:00:00Z",
  }),
];
const BETA = [
  projectedTerminal({
    id: "bb-0001",
    tmux_session: "cao-beta",
    session_name: "cao-beta",
    agent_profile: "spec-writer",
    provider: "kimi_cli",
    status: "processing",
    last_active: "2026-07-27T12:00:00Z",
  }),
  projectedTerminal({
    id: "bb-0002",
    tmux_session: "cao-beta",
    session_name: "cao-beta",
    agent_profile: "reviewer",
    provider: "kimi_cli",
    status: "not_fifo_monitored",
    last_active: "2026-07-27T11:00:00Z",
  }),
];
const ALL = [...ALPHA, ...BETA];

const FUTURE = "2999-01-01T00:00:00Z";

function note(
  t: (typeof ALL)[number],
  label: string,
  details: Record<string, string>,
  priority = 60,
): Annotation {
  return {
    namespace: "cao-conductor",
    kind: "display",
    version: 1,
    label,
    semantic_role: "warning",
    priority,
    subject: { type: "terminal", terminal_id: t.id, generation: t.generation },
    valid_until: FUTURE,
    details,
  };
}

// One of every control shape: phase/attention are SHARED vocabularies across
// the two sessions (global pills), lane is PARTITIONED per campaign
// (per-session pills), parked_at is a timestamp (range), and publication.pr
// rides exactly one session (the §5.4 case).
const ANNOTATIONS: AnnotationsResponse = {
  annotation_schema: "cao-annotations-v1",
  coverage: "complete",
  sources_read: 1,
  sources_failed: 0,
  items_dropped: 0,
  items_omitted: 0,
  reasons: [],
  annotations: [
    note(ALPHA[0], "reported", {
      phase: "reported",
      attention: "needs-review",
      lane: "l-01",
      parked_at: "2026-07-20T10:00:00Z",
    }, 90),
    note(ALPHA[1], "waiting", { phase: "waiting", attention: "none", lane: "l-02" }, 80),
    note(BETA[0], "reported", { phase: "reported", attention: "needs-review", lane: "l-03" }, 90),
    note(BETA[1], "pr open", { "publication.pr": "pr17 open", lane: "l-03" }, 70),
  ],
};

async function stubFleet(page: Page) {
  await stubBackend(page, {
    sessions: SESSIONS,
    terminalsBySession: { "cao-alpha": ALPHA, "cao-beta": BETA, "cao-empty": [] },
    annotations: ANNOTATIONS,
  });
}

function card(page: Page, name: string): Locator {
  return page.locator(`#session-${name}-terminals`).locator("..");
}

async function visibleIds(page: Page, name: string): Promise<string[]> {
  const region = page.locator(`#session-${name}-terminals`);
  if ((await region.count()) === 0) return [];
  const out: string[] = [];
  for (const t of ALL) {
    if ((await region.getByText(t.id.slice(0, 8), { exact: true }).count()) > 0) out.push(t.id);
  }
  return out;
}

/** The global panel starts collapsed below sm; expanding it is a no-op on desktop. */
async function ensurePanelOpen(page: Page) {
  if ((await page.locator("#global-filter-panel").count()) === 0) {
    await page.getByRole("button", { name: /Filters/ }).click();
  }
  await expect(page.locator("#global-filter-panel")).toBeVisible();
}

/**
 * Every interactive element inside `selector` meets the AAA 44×44 target —
 * the same measurement the workstate suite applies to the annotation
 * surfaces, here applied to the bars this feature ADDS. The count is
 * asserted nonzero by the caller: the bars are full of controls, and a zero
 * would mean the selector measured nothing.
 */
async function assertTargetSize(page: Page, selector: string): Promise<number> {
  const controls = page.locator(
    `${selector} button, ${selector} a, ${selector} input, ${selector} select, ${selector} [role="button"], ${selector} [tabindex]:not([tabindex="-1"])`,
  );
  const count = await controls.count();
  for (let i = 0; i < count; i += 1) {
    const box = await controls.nth(i).boundingBox();
    if (!box) continue;
    expect(box.width, `WCAG 2.5.5 AAA target width for control #${i} in ${selector}`).toBeGreaterThanOrEqual(44);
    expect(box.height, `WCAG 2.5.5 AAA target height for control #${i} in ${selector}`).toBeGreaterThanOrEqual(44);
  }
  return count;
}

test.beforeEach(async ({ page }) => {
  await stubFleet(page);
  await page.goto("/");
  // The chips are the third, independent fetch; waiting for one means the
  // annotation-derived dimensions are computed too.
  await expect(page.getByTestId("annotation-chip").first()).toBeVisible();
});

test("reachability is multi-select: OR within, AND across — and reachable without opening the panel", async ({
  page,
}) => {
  // The reachability row is OUTSIDE the collapsible region, so this works at
  // 390px without touching the toggle — the pinned contract.
  await page.getByRole("button", { name: "Managed Live" }).click();
  await page.getByRole("button", { name: "Idle" }).click();
  await expect.poll(() => visibleIds(page, "cao-alpha")).toEqual(["aa-0001", "aa-0002", "aa-0003"]);
  await expect.poll(() => visibleIds(page, "cao-beta")).toEqual(["bb-0002"]);

  // AND across dimensions: the spec-writer profile leaves only bb-0002's card.
  await ensurePanelOpen(page);
  await page.locator("#global-filter-panel").getByRole("button", { name: "reviewer" }).click();
  await expect.poll(() => visibleIds(page, "cao-alpha")).toEqual([]);
  // alpha still renders: global filters gate session visibility…
  await expect(page.locator("#session-cao-alpha-terminals")).toHaveCount(0);
  await expect.poll(() => visibleIds(page, "cao-beta")).toEqual(["bb-0002"]);

  await page.getByRole("button", { name: "Clear all" }).click();
  await expect.poll(() => visibleIds(page, "cao-alpha")).toEqual(ALPHA.map((t) => t.id));
});

test("shared facets go global, partitioned facets stay in their session, and PR state is never fleet-wide", async ({
  page,
}, testInfo) => {
  await ensurePanelOpen(page);
  const panel = page.locator("#global-filter-panel");

  // phase and attention are emitted in both sessions with a SHARED
  // vocabulary: they are the global derived dimensions, with counts.
  await expect(panel.getByText("phase", { exact: true })).toBeVisible();
  await expect(panel.getByText("attention", { exact: true })).toBeVisible();
  await expect(panel.getByRole("button", { name: /^reported/ })).toContainText("2");

  // lane is PARTITIONED per campaign, and publication.pr rides one session:
  // neither may present as a fleet-wide authoritative filter (§5.4).
  await expect(panel.getByText("lane", { exact: true })).toHaveCount(0);
  await expect(panel.getByText("pr", { exact: true })).toHaveCount(0);
  await expect(card(page, "cao-alpha").getByText("lane", { exact: true })).toBeVisible();
  await expect(card(page, "cao-beta").getByText("pr", { exact: true })).toBeVisible();
  await expect(card(page, "cao-alpha").getByText("pr", { exact: true })).toHaveCount(0);

  // The global selection gates session visibility and the counter counts the
  // view, never the summary.
  await panel.getByRole("button", { name: /^reported/ }).click();
  await expect.poll(() => visibleIds(page, "cao-alpha")).toEqual(["aa-0001"]);
  await expect.poll(() => visibleIds(page, "cao-beta")).toEqual(["bb-0001"]);
  await expect(card(page, "cao-alpha").getByTestId("session-filter-count")).toHaveText("1 of 4 shown");

  // The summary still describes the session: 4 agents, not 1.
  await expect(card(page, "cao-alpha").getByText("4 agents")).toBeVisible();

  // The timestamp facet earned a range control inside alpha's card only.
  await expect(card(page, "cao-alpha").getByLabel("parked at from")).toBeVisible();
  await expect(card(page, "cao-beta").getByLabel("parked at from")).toHaveCount(0);

  await page.screenshot({
    path: `${SHOTS}/${testInfo.project.name}-global-phase-reported.png`,
    fullPage: true,
  });
});

test("a per-session filter that matches nothing keeps the card and recovers in one click", async ({
  page,
}) => {
  const alpha = card(page, "cao-alpha");
  await alpha.getByLabel("Session filter text").fill("zzz-no-match");
  await expect.poll(() => visibleIds(page, "cao-alpha")).toEqual([]);
  await expect(alpha.getByTestId("session-filter-count")).toHaveText("0 of 4 shown");
  await expect(alpha.getByText("the session filters hide every row")).toBeVisible();
  // The card itself is never removed by its own session bar…
  await expect(page.locator("#session-cao-alpha-terminals")).toHaveCount(1);
  // …while the untouched card keeps its rows.
  await expect.poll(() => visibleIds(page, "cao-beta")).toEqual(BETA.map((t) => t.id));

  await alpha.getByRole("button", { name: "Clear session filters" }).first().click();
  await expect.poll(() => visibleIds(page, "cao-alpha")).toEqual(ALPHA.map((t) => t.id));
});

test("free text is case-insensitive end to end, at both bars", async ({ page }) => {
  await ensurePanelOpen(page);
  const globalText = page.locator("#global-filter-panel").getByLabel("Filter text", { exact: true });
  await globalText.fill("  SPEC-WRITER  ");
  await expect(page.locator("#session-cao-alpha-terminals")).toHaveCount(0);
  await expect.poll(() => visibleIds(page, "cao-beta")).toEqual(["bb-0001"]);
  await globalText.fill("");

  // The per-session box narrows inside its own card only.
  await card(page, "cao-alpha").getByLabel("Session filter text").fill("NEEDS-REVIEW");
  await expect.poll(() => visibleIds(page, "cao-alpha")).toEqual(["aa-0001"]);
  await expect.poll(() => visibleIds(page, "cao-beta")).toEqual(BETA.map((t) => t.id));
});

test("every control the bar adds meets the 44px AAA target, and the bar scans clean", async ({
  page,
}) => {
  await ensurePanelOpen(page);
  // Nonzero counts asserted: the bars are full of controls, and a vacuous
  // measurement would read as a pass.
  expect(await assertTargetSize(page, '[data-testid="filter-bar"]')).toBeGreaterThan(10);
  expect(await assertTargetSize(page, '[data-testid="session-filter-bar"]')).toBeGreaterThan(3);

  const scan = await new AxeBuilder({ page })
    .include('[data-testid="filter-bar"]')
    .include('[data-testid="session-filter-bar"]')
    .analyze();
  expect(
    scan.violations.filter((v) => v.impact === "serious" || v.impact === "critical"),
  ).toEqual([]);
});

test("the bar wraps at the phone width instead of eating it", async ({ page }, testInfo) => {
  await ensurePanelOpen(page);
  // Nothing sticks out sideways: the bar grows DOWN, never off the card edge.
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow, "horizontal overflow from the filter bar").toBeLessThanOrEqual(1);

  // Collapsed, the bar leaves the fleet on screen: one row of header plus the
  // reachability row, not half a phone screen.
  if (testInfo.project.name === "mobile-chromium") {
    await page.reload();
    await expect(page.getByTestId("annotation-chip").first()).toBeVisible();
    await expect(page.locator("#global-filter-panel")).toHaveCount(0);
    const barBox = await page.locator('[data-testid="filter-bar"]').boundingBox();
    expect(barBox!.height, "collapsed filter bar stays well under half the phone screen").toBeLessThan(300);
  }

  await page.screenshot({
    path: `${SHOTS}/${testInfo.project.name}-bar-wrapped.png`,
    fullPage: false,
  });
});
