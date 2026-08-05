import { test, expect, Page, Locator } from "@playwright/test";
import { AxeBuilder } from "@axe-core/playwright";
import type { Annotation, AnnotationsResponse } from "../src/api";
import { projectedTerminal } from "../src/test/projectedTerminal";
import { stubBackend } from "./stub";

// Chip-bar e2e evidence, at 1280×800 and 390×844 (both configured projects).
// The stub serves a REAL fleet for these specs — several sessions, several
// profiles, several statuses, and annotations carrying one of every control
// shape and one of every RANKING shape — because the default one-terminal
// stub makes every filter assertion vacuous.
//
// The bar this covers: only ACTIVE filters render as chips (`Dimension:
// selection`), a dashed "+ Filter" opens a picker ranked by derived
// usefulness, and an Advanced sheet holds everything, including the
// dimensions the picker demoted or omitted.

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
  kind = "display",
): Annotation {
  return {
    namespace: "cao-conductor",
    kind,
    version: 1,
    label,
    semantic_role: "warning",
    priority,
    subject: { type: "terminal", terminal_id: t.id, generation: t.generation },
    valid_until: FUTURE,
    details,
  };
}

/** An identity annotation: the lane label is the point of it. */
function lane(t: (typeof ALL)[number], label: string, colourKey: string): Annotation {
  return {
    ...note(t, label, {}, 8, "lane"),
    semantic_role: "neutral",
    colour_key: colourKey,
  };
}

// One of every control shape AND one of every ranking shape. phase/attention
// are SHARED vocabularies across the two sessions (global primary pills);
// lane is PARTITIONED per campaign (per-session pills); parked_at is a
// timestamp (range); publication.pr rides exactly one session (the §5.4
// case); lane_source is one value on every row (omitted from every picker —
// selecting it could only ever pass); commit is forty hex chars per row
// (opaque, demoted below the picker's fold); and the `lane` KIND carries the
// lane identity as its label — the dimension free text used to answer alone.
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
      lane_source: "task-prefix",
      commit: "0e63aac5f65421ff481a7186b3f2e8de5030fd52",
    }, 90),
    note(ALPHA[1], "waiting", {
      phase: "waiting",
      attention: "none",
      lane: "l-02",
      lane_source: "task-prefix",
      commit: "1f74bbf6e65532ff591b8296c4f3e9de5141ae31",
    }, 80),
    note(ALPHA[2], "waiting", { lane_source: "task-prefix" }, 50),
    note(ALPHA[3], "reported", { lane_source: "task-prefix" }, 40),
    note(BETA[0], "reported", {
      phase: "reported",
      attention: "needs-review",
      lane: "l-03",
      lane_source: "task-prefix",
      commit: "2a85cce70f7664300692c93a07e5040f06251bf42",
    }, 90),
    note(BETA[1], "pr open", {
      "publication.pr": "pr17 open",
      lane: "l-03",
      lane_source: "task-prefix",
      commit: "3b96ddf81a87754117a3d4b18f06151f17362c053",
    }, 70),
    lane(ALPHA[0], "cond-0001", "lane-token-a"),
    lane(ALPHA[1], "cond-0001", "lane-token-a"),
    lane(BETA[0], "cond-0002", "lane-token-b"),
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

// ── Chip-bar drivers ──────────────────────────────────────────────────────
// The popovers are portalled to document.body, so they are fetched by their
// testids; only the trigger buttons are looked up inside the bar/card.

/** Open a bar's "+ Filter" picker and choose a dimension by its key. */
async function addFilter(page: Page, idPrefix: string, dimension: string): Promise<Locator> {
  await page.getByTestId(`${idPrefix}-picker-button`).click();
  const picker = page.getByTestId(`${idPrefix}-picker`);
  await picker.locator(`[data-dimension="${dimension}"]`).click();
  return page.getByTestId(`${idPrefix}-editor`);
}

/** The chip one dimension wears in a bar, active or pinned by its editor. */
function chip(page: Page, idPrefix: string, dimension: string): Locator {
  return page.locator(`[data-testid="${idPrefix}-chip"][data-dimension="${dimension}"]`);
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

test("reachability is a chip: multi-select OR within, AND across with a second dimension", async ({
  page,
}) => {
  const editor = await addFilter(page, "global", "reachability");
  await editor.getByRole("button", { name: "Managed Live" }).click();
  await editor.getByRole("button", { name: "Idle" }).click();
  await expect.poll(() => visibleIds(page, "cao-alpha")).toEqual(["aa-0001", "aa-0002", "aa-0003"]);
  await expect.poll(() => visibleIds(page, "cao-beta")).toEqual(["bb-0002"]);

  // The chip reads the selection, not just the dimension name.
  await expect(chip(page, "global", "reachability")).toContainText("Reachability: Managed Live, Idle");

  // AND across dimensions: the reviewer profile leaves only bb-0002's card.
  const profiles = await addFilter(page, "global", "profiles");
  await profiles.getByRole("button", { name: "reviewer" }).click();
  await expect.poll(() => visibleIds(page, "cao-alpha")).toEqual([]);
  await expect(page.locator("#session-cao-alpha-terminals")).toHaveCount(0);
  await expect.poll(() => visibleIds(page, "cao-beta")).toEqual(["bb-0002"]);

  await page.getByRole("button", { name: "Clear all" }).click();
  await expect.poll(() => visibleIds(page, "cao-alpha")).toEqual(ALPHA.map((t) => t.id));
});

test("the picker offers dimensions by shape: shared go global, partitioned stay local, noise is omitted", async ({
  page,
}) => {
  await page.getByTestId("global-picker-button").click();
  const globalPicker = page.getByTestId("global-picker");

  // phase and attention are emitted in both sessions with a SHARED
  // vocabulary: they are the global derived dimensions.
  await expect(globalPicker.locator('[data-dimension="phase"]')).toBeVisible();
  await expect(globalPicker.locator('[data-dimension="attention"]')).toBeVisible();

  // lane is PARTITIONED per campaign, and publication.pr rides one session:
  // neither may present as a fleet-wide authoritative filter (§5.4).
  await expect(globalPicker.locator('[data-dimension="lane"]')).toHaveCount(0);
  await expect(globalPicker.locator('[data-dimension="publication.pr"]')).toHaveCount(0);

  // lane_source is one value on every one of the six rows: selecting it could
  // only ever pass, so no picker offers it. It still exists — see the
  // Advanced assertions below.
  await expect(globalPicker.locator('[data-dimension="lane_source"]')).toHaveCount(0);
  await page.keyboard.press("Escape");

  // The session pickers hold the session-local vocabularies.
  await card(page, "cao-alpha").getByTestId("session-cao-alpha-picker-button").click();
  const alphaPicker = page.getByTestId("session-cao-alpha-picker");
  await expect(alphaPicker.locator('[data-dimension="lane"]')).toBeVisible();
  await expect(alphaPicker.locator('[data-dimension="label:lane"]')).toBeVisible();
  await expect(alphaPicker.locator('[data-dimension="parked_at"]')).toBeVisible();
  await expect(alphaPicker.locator('[data-dimension="publication.pr"]')).toHaveCount(0);
  await expect(alphaPicker.locator('[data-dimension="lane_source"]')).toHaveCount(0);
  // commit is forty hex characters a row: offered, but demoted below the fold
  // under the "less useful" heading, never as a pill wall of hashes.
  const commitItem = alphaPicker.locator('[data-dimension="commit"]');
  await expect(commitItem).toBeVisible();
  await expect(commitItem).toContainText("opaque");
  await page.keyboard.press("Escape");

  await card(page, "cao-beta").getByTestId("session-cao-beta-picker-button").click();
  const betaPicker = page.getByTestId("session-cao-beta-picker");
  await expect(betaPicker.locator('[data-dimension="publication.pr"]')).toBeVisible();
  await page.keyboard.press("Escape");

  // The Advanced sheet is where the omitted and the demoted still live.
  await page.getByTestId("global-advanced-button").click();
  const advanced = page.getByTestId("global-advanced");
  await expect(advanced.getByText("lane source", { exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
});

test("a global selection gates session visibility, and the counter counts the view, never the summary", async ({
  page,
}, testInfo) => {
  const editor = await addFilter(page, "global", "phase");
  // Value counts ride the options.
  await expect(editor.getByRole("button", { name: /^reported/ })).toContainText("2");
  await editor.getByRole("button", { name: /^reported/ }).click();
  await expect.poll(() => visibleIds(page, "cao-alpha")).toEqual(["aa-0001"]);
  await expect.poll(() => visibleIds(page, "cao-beta")).toEqual(["bb-0001"]);
  await expect(card(page, "cao-alpha").getByTestId("session-filter-count")).toHaveText("1 of 4 shown");

  // The summary still describes the session: 4 agents, not 1.
  await expect(card(page, "cao-alpha").getByText("4 agents")).toBeVisible();

  // The timestamp facet earned a range control in alpha's editor, and beta
  // has no such dimension at all.
  const alphaRange = await addFilter(page, "session-cao-alpha", "parked_at");
  await expect(alphaRange.getByLabel("parked at from")).toBeVisible();
  await page.keyboard.press("Escape");
  await card(page, "cao-beta").getByTestId("session-cao-beta-picker-button").click();
  await expect(page.getByTestId("session-cao-beta-picker").locator('[data-dimension="parked_at"]')).toHaveCount(0);
  await page.keyboard.press("Escape");

  await page.screenshot({
    path: `${SHOTS}/${testInfo.project.name}-global-phase-reported.png`,
    fullPage: true,
  });
});

test("chip lifecycle: add from the picker, edit in the popover, remove with the X", async ({ page }) => {
  // Add.
  const editor = await addFilter(page, "global", "phase");
  await editor.getByRole("button", { name: /^reported/ }).click();
  await expect(chip(page, "global", "phase")).toContainText("phase: reported");
  await expect.poll(() => visibleIds(page, "cao-alpha")).toEqual(["aa-0001"]);

  // Edit: the editor stays open across toggles, so a second value joins the
  // OR — and the chip's text tracks the selection.
  await editor.getByRole("button", { name: /^waiting/ }).click();
  await expect(chip(page, "global", "phase")).toContainText("phase: reported, waiting");
  await expect.poll(() => visibleIds(page, "cao-alpha")).toEqual(["aa-0001", "aa-0002"]);
  await page.keyboard.press("Escape");

  // Remove with the X (hidden until hover on pointer devices, always visible
  // under hover:none).
  const phaseChip = chip(page, "global", "phase");
  await phaseChip.hover();
  await phaseChip.getByRole("button", { name: "Remove phase filter" }).click();
  await expect(chip(page, "global", "phase")).toHaveCount(0);
  await expect.poll(() => visibleIds(page, "cao-alpha")).toEqual(ALPHA.map((t) => t.id));
});

test("the lane label dimension narrows the session to one lane", async ({ page }) => {
  // Fault D: the lane identity is the annotation's LABEL, and it is a real
  // dimension now — offered by the session's picker, narrowing by it.
  const editor = await addFilter(page, "session-cao-alpha", "label:lane");
  await editor.getByRole("button", { name: /^cond-0001/ }).click();

  const laneChip = chip(page, "session-cao-alpha", "label:lane");
  await expect(laneChip).toContainText("lane: cond-0001");
  await expect.poll(() => visibleIds(page, "cao-alpha")).toEqual(["aa-0001", "aa-0002"]);
  await expect(card(page, "cao-alpha").getByTestId("session-filter-count")).toHaveText("2 of 4 shown");
  // The other session is untouched: the session bar is AND-ed inside its card.
  await expect.poll(() => visibleIds(page, "cao-beta")).toEqual(BETA.map((t) => t.id));

  await page.keyboard.press("Escape");
  await laneChip.hover();
  await laneChip.getByRole("button", { name: "Remove lane filter" }).click();
  await expect.poll(() => visibleIds(page, "cao-alpha")).toEqual(ALPHA.map((t) => t.id));
});

test("the advanced sheet holds everything, traps focus, and closes on Escape", async ({ page }) => {
  await page.getByTestId("global-advanced-button").click();
  const advanced = page.getByTestId("global-advanced");

  // The demoted and the omitted are all here: the single-value lane_source
  // (fleet-wide by the sharing rule, picker-omitted because selecting it
  // could only ever pass) and the fork-owned dimensions.
  await expect(advanced.getByText("lane source", { exact: true })).toBeVisible();
  await expect(advanced.getByText("Reachability", { exact: true })).toBeVisible();

  // Focus starts inside the dialog and Tab never leaves it.
  await expect
    .poll(() =>
      page.evaluate(() => document.activeElement?.closest('[data-testid="global-advanced"]') !== null),
    )
    .toBe(true);
  for (let i = 0; i < 12; i += 1) await page.keyboard.press("Tab");
  await expect
    .poll(() =>
      page.evaluate(() => document.activeElement?.closest('[data-testid="global-advanced"]') !== null),
    )
    .toBe(true);

  await page.keyboard.press("Escape");
  await expect(advanced).toHaveCount(0);
  await expect(page.getByTestId("global-advanced-button")).toBeFocused();

  // The same component serves the session bar: the sheet there holds the
  // session-local dimensions, the label family, and the spawned-by row. The
  // opaque commit is session-local (a text control never goes fleet-wide), so
  // its assertion lives here, not in the global sheet.
  await card(page, "cao-alpha").getByTestId("session-cao-alpha-advanced-button").click();
  const sessionAdvanced = page.getByTestId("session-cao-alpha-advanced");
  await expect(sessionAdvanced.getByText("Spawned by", { exact: true })).toBeVisible();
  await expect(sessionAdvanced.getByText("Annotation labels", { exact: true })).toBeVisible();
  await expect(sessionAdvanced.getByLabel("commit contains")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(sessionAdvanced).toHaveCount(0);
});

test("the default bar is one row at both viewports — evidence screenshots", async ({ page }, testInfo) => {
  const bar = page.getByTestId("filter-bar");
  // Nothing active: no chips, just search + Filter + Advanced.
  await expect(bar.locator('[data-testid="global-chip"]')).toHaveCount(0);
  const barBox = await bar.boundingBox();
  expect(barBox!.height, "the default global bar is a single 44px row plus padding").toBeLessThanOrEqual(64);

  const sessionBar = card(page, "cao-alpha").getByTestId("session-filter-bar");
  const sessionBox = await sessionBar.boundingBox();
  expect(sessionBox!.height, "the default session bar is a single row too").toBeLessThanOrEqual(64);

  await page.screenshot({ path: `${SHOTS}/${testInfo.project.name}-bar-default.png`, fullPage: false });

  // Three chips active: the bar wraps at 390px — it does not stack into a
  // form, and nothing leaves the viewport sideways.
  const reachability = await addFilter(page, "global", "reachability");
  await reachability.getByRole("button", { name: "Managed Live" }).click();
  const phase = await addFilter(page, "global", "phase");
  await phase.getByRole("button", { name: /^reported/ }).click();
  const attention = await addFilter(page, "global", "attention");
  await attention.getByRole("button", { name: /^needs-review/ }).click();
  await expect(bar.locator('[data-testid="global-chip"]')).toHaveCount(3);
  // Close the editor before the evidence shot: the popover would otherwise
  // cover the wrapped chips it just proved exist.
  await page.keyboard.press("Escape");

  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow, "horizontal overflow from the chip bar").toBeLessThanOrEqual(1);

  await page.screenshot({ path: `${SHOTS}/${testInfo.project.name}-bar-three-chips.png`, fullPage: false });
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
  const globalText = page.getByTestId("filter-bar").getByLabel("Filter text", { exact: true });
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
  // Nonzero counts asserted: the bars are full of controls, and a vacuous
  // measurement would read as a pass. Chips, the X on each chip, picker
  // items, popover options and modal rows are all measured here.
  expect(await assertTargetSize(page, '[data-testid="filter-bar"]')).toBeGreaterThan(2);
  expect(await assertTargetSize(page, '[data-testid="session-filter-bar"]')).toBeGreaterThan(2);

  await page.getByTestId("global-picker-button").click();
  expect(await assertTargetSize(page, '[data-testid="global-picker"]')).toBeGreaterThan(5);
  await page.getByTestId("global-picker").locator('[data-dimension="phase"]').click();
  expect(await assertTargetSize(page, '[data-testid="global-editor"]')).toBeGreaterThan(1);
  // With a chip active, its X is a real 44×44 too.
  await page.getByTestId("global-editor").getByRole("button", { name: /^reported/ }).click();
  expect(await assertTargetSize(page, '[data-testid="filter-bar"]')).toBeGreaterThan(4);
  await page.keyboard.press("Escape");

  await page.getByTestId("global-advanced-button").click();
  expect(await assertTargetSize(page, '[data-testid="global-advanced"]')).toBeGreaterThan(10);
  await page.keyboard.press("Escape");

  const scan = await new AxeBuilder({ page })
    .include('[data-testid="filter-bar"]')
    .include('[data-testid="session-filter-bar"]')
    .analyze();
  expect(
    scan.violations.filter((v) => v.impact === "serious" || v.impact === "critical"),
  ).toEqual([]);
});
