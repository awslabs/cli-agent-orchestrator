import { test, expect, Page } from "@playwright/test";
import { AxeBuilder } from "@axe-core/playwright";
import type { Annotation, AnnotationsResponse } from "../src/api";
import { projectedTerminal } from "../src/test/projectedTerminal";
import { stubBackend, stubTerminals, T_NATIVE_GENERATION } from "./stub";

// A3 visual evidence: conductor annotation chips (work-state design §9.5),
// captured at 1280×800 and 390×844 (both configured projects) against the
// stubbed server. Screenshots land under e2e/__screenshots__/workstate/ so the
// A3 evidence set is one directory an operator can page through.
//
// The accessibility gate is deliberately two gates, labelled honestly:
//   * axe-core with zero serious/critical violations — WCAG 2.x A/AA;
//   * every interactive element in the annotation surface ≥44×44 CSS px, which
//     is WCAG 2.5.5 Target Size (Enhanced) **AAA** (and Apple's default). The
//     AA floor is 2.5.8's 24×24; the stricter number is retained on purpose
//     for a touch-operated dashboard (§13.8) and is NOT what the axe gate
//     measures.

const SHOTS = "e2e/__screenshots__/workstate";

/**
 * No terminal row hides content off the right edge of its card.
 *
 * `document.scrollWidth` stays at the viewport width when a flex child
 * overflows a `min-w-0` container, so there is no scrollbar to reveal what was
 * cut — the content is simply gone. Measured per row instead.
 */
async function assertNoHorizontalClipping(page: Page) {
  const overflow = await page.evaluate(() => {
    const rows = Array.from(
      document.querySelectorAll<HTMLElement>("#session-cao-fleet-terminals .flex.min-w-0"),
    );
    return rows
      .map((row) => ({
        text: (row.textContent ?? "").slice(0, 60),
        client: row.clientWidth,
        scroll: row.scrollWidth,
      }))
      .filter((r) => r.scroll > r.client + 1);
  });
  expect(overflow, "identity rows clipping content off the card").toEqual([]);
}

const FUTURE = "2999-01-01T00:00:00Z";
const LONG_PAST = "2020-01-01T00:00:00Z";

function hoursAgo(hours: number): string {
  return new Date(Date.now() - hours * 3600_000).toISOString();
}

function annotation(overrides: Partial<Annotation> = {}): Annotation {
  return {
    namespace: "cao-conductor",
    kind: "work-state.display",
    version: 1,
    label: "waiting",
    semantic_role: "warning",
    priority: 60,
    subject: {
      type: "terminal",
      terminal_id: "t-native",
      generation: T_NATIVE_GENERATION,
    },
    valid_until: FUTURE,
    details: {},
    source: "aegix-mobile-phase0-renewal",
    ...overrides,
  };
}

/**
 * An identity chip: `annotation()` plus the one opaque field that makes it one.
 *
 * `colour_key` is the ONLY difference. Nothing in the renderer reads `kind`,
 * so the kinds below are labels for the reader of this file and nothing more.
 */
function identity(overrides: Partial<Annotation> = {}): Annotation {
  return annotation({
    kind: "lane",
    label: "pr04",
    semantic_role: "neutral",
    priority: 8,
    colour_key: "lane-alpha",
    details: {},
    ...overrides,
  });
}

/** `t-native` plus `count - 1` extra rows sharing its agent type. */
function fleet(count: number) {
  return [
    stubTerminals[0],
    ...Array.from({ length: count - 1 }, (_, i) =>
      projectedTerminal({
        id: `t-${i + 1}`,
        tmux_window: `${i + 1}`,
        provider: "kimi_cli",
        agent_profile: "spec-writer-k3",
        status: "not_fifo_monitored",
      }),
    ),
  ];
}

/** The subject naming row `id`, fenced on the generation `projectedTerminal` gives it. */
function onRow(id: string): Annotation["subject"] {
  return {
    type: "terminal",
    terminal_id: id,
    generation: id === "t-native" ? T_NATIVE_GENERATION : `${id}-gen-1`,
  };
}

function payload(
  annotations: Annotation[],
  overrides: Partial<AnnotationsResponse> = {},
): AnnotationsResponse {
  return {
    annotation_schema: "cao-annotations-v1",
    coverage: "complete",
    sources_read: 1,
    sources_failed: 0,
    items_dropped: 0,
    items_omitted: 0,
    reasons: [],
    annotations,
    ...overrides,
  };
}

/**
 * Every interactive element inside `selector` meets the AAA 44×44 target.
 *
 * Returns the count so the CALLER can assert on it. The chips are deliberately
 * non-interactive, so this loop measures nothing today and a bare call reads as
 * a passing measurement when it is really a vacuous one. Asserting the count is
 * zero makes the day somebody adds a control change loudly.
 */
async function assertTargetSize(page: Page, selector: string) {
  const controls = page.locator(
    `${selector} button, ${selector} a, ${selector} input, ${selector} [role="button"], ${selector} [tabindex]:not([tabindex="-1"])`,
  );
  const count = await controls.count();
  for (let i = 0; i < count; i += 1) {
    const box = await controls.nth(i).boundingBox();
    if (!box) continue;
    expect(box.width, "WCAG 2.5.5 AAA target width").toBeGreaterThanOrEqual(44);
    expect(box.height, "WCAG 2.5.5 AAA target height").toBeGreaterThanOrEqual(44);
  }
  return count;
}

/**
 * Serious/critical axe results on the annotation surfaces — violations AND
 * incomplete.
 *
 * `scan.incomplete` IS NOT A PASS. Reading only `scan.violations` let a
 * serious-impact `aria-prohibited-attr` firing on 100% of the chips through
 * both this gate and the whole-page one: axe files "aria-label on a role-less
 * <span>" as incomplete because it cannot verify the AT behaviour, and an
 * incomplete on an element THIS CHANGE INTRODUCED is a finding, not a pass.
 *
 * SCOPED, like every other axe assertion in this suite (see dashboard.spec.ts).
 * The whole-page scan is not zero and never has been: the pre-existing
 * dashboard chrome uses `text-gray-500`/`text-gray-600` on `#0f0f14` and
 * `bg-emerald-600` buttons, which axe flags for contrast on the no-annotations
 * control too. Asserting whole-page zero here would either fail on somebody
 * else's defect or, worse, be "fixed" by restyling unrelated chrome inside an
 * annotation change. `test("adds no new accessibility violation …")` below
 * covers the other half honestly: the whole-page result set must be IDENTICAL
 * with and without annotations.
 */
async function assertNoSeriousAxeViolations(page: Page, ...include: string[]) {
  let builder = new AxeBuilder({ page });
  for (const selector of include) builder = builder.include(selector);
  const scan = await builder.analyze();
  // The messageKey is included because "color-contrast: <selector>" does not
  // say WHY, and the why decides the fix: `bgGradient`/`bgOverlap` mean axe
  // could not composite the backdrop, `shortTextContent` means it declined to
  // judge, and a bare contrast failure means the colours are actually wrong.
  const serious = (results: typeof scan.violations) =>
    results
      .filter((v) => v.impact === "serious" || v.impact === "critical")
      .flatMap((v) =>
        v.nodes.map((n) => {
          const why = [...n.any, ...n.all, ...n.none]
            .map((c) => (c.data as { messageKey?: string } | undefined)?.messageKey)
            .filter(Boolean)[0];
          return `${v.id}${why ? `[${why}]` : ""}: ${n.target.join(" ")}`;
        }),
      );
  expect(serious(scan.violations), "axe violations").toEqual([]);
  expect(serious(scan.incomplete), "axe incomplete").toEqual([]);
}

/**
 * The same gate, minus specific axe *measurement-limitation* reason codes.
 *
 * Only ever legitimate when the thing the code prevents axe from judging is
 * being judged some other way — see the `assertCompositedContrast` call that
 * accompanies every use.
 */
async function assertAxeExcept(page: Page, tolerate: string[], ...include: string[]) {
  let builder = new AxeBuilder({ page });
  for (const selector of include) builder = builder.include(selector);
  const scan = await builder.analyze();
  const serious = (results: typeof scan.violations) =>
    results
      .filter((v) => v.impact === "serious" || v.impact === "critical")
      .flatMap((v) =>
        v.nodes.map((n) => {
          const why = [...n.any, ...n.all, ...n.none]
            .map((c) => (c.data as { messageKey?: string } | undefined)?.messageKey)
            .filter(Boolean)[0];
          return { key: `${v.id}${why ? `[${why}]` : ""}: ${n.target.join(" ")}`, why };
        }),
      )
      .filter((r) => !r.why || !tolerate.includes(r.why))
      .map((r) => r.key);
  expect(serious(scan.violations), "axe violations").toEqual([]);
  expect(serious(scan.incomplete), "axe incomplete").toEqual([]);
}

/**
 * Composited contrast for every text node under `selector`, checked ourselves.
 *
 * WHY THIS EXISTS RATHER THAN A WIDER axe SCOPE. axe will not certify contrast
 * on an element that partially overlaps others — it returns
 * `color-contrast[elmPartiallyObscuring]`, meaning "I cannot determine the
 * backdrop", not "the contrast is wrong". A `position: fixed` popover overlaps
 * page content by construction, so on the 390×844 project the card's footer is
 * permanently uncertifiable no matter how opaque it is made.
 *
 * Tolerating that reason code alone would be a hole: it is indistinguishable
 * from a genuine failure axe merely could not measure. So instead of relaxing
 * the gate we do the measurement axe declined to do — walk the ancestor chain
 * compositing every semi-transparent layer to an opaque backdrop, then apply
 * the WCAG AA ratio. That is strictly stronger than the check it replaces.
 */
async function assertCompositedContrast(page: Page, selector: string) {
  const failures = await page.evaluate((sel) => {
    const parse = (c: string) => {
      const m = c.match(/rgba?\(([^)]+)\)/);
      if (!m) return null;
      const p = m[1].split(",").map((s) => parseFloat(s.trim()));
      return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
    };
    type C = { r: number; g: number; b: number; a: number };
    const over = (fg: C, bg: C): C => ({
      r: fg.r * fg.a + bg.r * (1 - fg.a),
      g: fg.g * fg.a + bg.g * (1 - fg.a),
      b: fg.b * fg.a + bg.b * (1 - fg.a),
      a: 1,
    });
    const lum = (c: C) => {
      const f = (v: number) => {
        v /= 255;
        return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
      };
      return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
    };
    const ratio = (a: C, b: C) => {
      const l1 = lum(a);
      const l2 = lum(b);
      return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    };
    const backdrop = (el: Element): C => {
      const stack: C[] = [];
      let node: Element | null = el;
      while (node && node !== document.documentElement) {
        const bg = parse(getComputedStyle(node).backgroundColor);
        if (bg && bg.a > 0) {
          stack.push(bg);
          if (bg.a === 1) break;
        }
        node = node.parentElement;
      }
      let base: C = { r: 255, g: 255, b: 255, a: 1 };
      for (let i = stack.length - 1; i >= 0; i--) base = over(stack[i], base);
      return base;
    };
    const root = document.querySelector(sel);
    if (!root) return [`selector matched nothing: ${sel}`];
    const bad: string[] = [];
    for (const el of Array.from(root.querySelectorAll("*"))) {
      if (el.children.length > 0) continue;
      const text = (el.textContent || "").trim();
      if (!text) continue;
      const cs = getComputedStyle(el);
      const fg = parse(cs.color);
      if (!fg) continue;
      const bg = backdrop(el);
      const size = parseFloat(cs.fontSize);
      const weight = parseInt(cs.fontWeight) || 400;
      const large = size >= 24 || (size >= 18.66 && weight >= 700);
      const need = large ? 3 : 4.5;
      const got = ratio(fg.a < 1 ? over(fg, bg) : fg, bg);
      if (got < need) bad.push(`${got.toFixed(2)}:1 < ${need}:1 — ${size}px "${text.slice(0, 24)}"`);
    }
    return bad;
  }, selector);
  expect(failures, `composited contrast under ${selector}`).toEqual([]);
}

/**
 * The whole page's serious/critical axe RULE IDS, from both buckets.
 *
 * Rule ids rather than node selectors: an axe target is a CSS path, so
 * inserting any element renumbers `:nth-child()` on unrelated pre-existing
 * violations and a node-level comparison would fail for a reason that has
 * nothing to do with accessibility. The question this answers is the one that
 * matters — "does rendering annotations make a new KIND of result appear?" —
 * and the scoped scans above answer "is any result inside the annotation
 * surfaces?"; between them that is complete.
 */
async function seriousViolationIds(page: Page): Promise<string[]> {
  const scan = await new AxeBuilder({ page }).analyze();
  return [
    ...new Set(
      [...scan.violations, ...scan.incomplete]
        .filter((v) => v.impact === "serious" || v.impact === "critical")
        .map((v) => v.id),
    ),
  ].sort();
}

test.describe("conductor annotation chips (§9.5)", () => {
  test("(a) a waiting worker shows its parked age beside the status badge", async ({
    page,
  }, testInfo) => {
    await stubBackend(page, {
      annotations: payload([
        annotation({
          kind: "work-state.display",
          label: "waiting",
          semantic_role: "warning",
          priority: 80,
          details: {
            task: "p0-09b-b0-b1-implementation-r1",
            role: "implementer",
            round: "12",
            parked_at: hoursAgo(58),
            lifecycle: "assigned",
            phase: "parked",
          },
        }),
      ]),
    });
    await page.goto("/");

    const chip = page.getByTestId("annotation-chip").first();
    await expect(chip).toBeVisible();
    await expect(chip).toContainText("waiting");
    // The parked age is on the chip's own face, not only in the hover.
    await expect(chip).toContainText(/2d/);
    // Alongside, never instead of: the fork's own badge is still on the row.
    // Two levels up — the chips live in their own group wrapper so they can
    // drop to a second line at 390px without shrinking the identity row.
    const row = chip.locator("xpath=../..");
    await expect(row.getByText("Managed Live")).toBeVisible();
    // THE ROW STILL SAYS WHICH WORKER IT IS. At 390 a single chip used to
    // delete the agent-profile name outright and clip the rest off the card.
    const profile = row.getByText("spec-writer-k3");
    await expect(profile).toBeVisible();
    const nameBox = await profile.boundingBox();
    expect(nameBox!.width, "agent profile name has a visible box").toBeGreaterThan(20);
    await assertNoHorizontalClipping(page);

    await page.screenshot({
      path: `${SHOTS}/${testInfo.project.name}-a-waiting-parked-age.png`,
      fullPage: true,
    });

    expect(
      await assertTargetSize(page, '[data-testid="annotation-chip"]'),
      "chips are deliberately non-interactive; a control here changes the claim",
    ).toBe(0);
    await assertNoSeriousAxeViolations(page, '[data-testid="annotation-chip"]');
  });

  test("(b) an active worker reads as active, in the info role", async ({
    page,
  }, testInfo) => {
    await stubBackend(page, {
      annotations: payload([
        annotation({
          kind: "work-state.display",
          label: "active",
          semantic_role: "info",
          priority: 90,
          details: {
            task: "p0-10-review-r2",
            role: "reviewer",
            round: "2",
            lifecycle: "assigned",
            phase: "in-round",
          },
        }),
      ]),
    });
    await page.goto("/");

    const chip = page.getByTestId("annotation-chip").first();
    await expect(chip).toBeVisible();
    await expect(chip).toHaveAttribute("data-role", "info");
    await expect(chip).toHaveAttribute("data-stale", "false");
    await expect(chip).toContainText("active");

    await page.screenshot({
      path: `${SHOTS}/${testInfo.project.name}-b-active-worker.png`,
      fullPage: true,
    });
    await assertNoSeriousAxeViolations(page, '[data-testid="annotation-chip"]');
  });

  test("(c) a stale annotation is greyed and says so", async ({ page }, testInfo) => {
    await stubBackend(page, {
      annotations: payload([
        annotation({
          kind: "work-state.display",
          label: "blocked",
          // A danger role that has expired: the whole point is that it does NOT
          // keep its alarming colour.
          semantic_role: "danger",
          priority: 95,
          valid_until: LONG_PAST,
          details: {
            task: "p0-09b-b0-b1-implementation-r1",
            lifecycle: "assigned",
            phase: "parked",
          },
        }),
      ]),
    });
    await page.goto("/");

    const chip = page.getByTestId("annotation-chip").first();
    await expect(chip).toBeVisible();
    await expect(chip).toHaveAttribute("data-stale", "true");
    await expect(chip).toHaveAttribute("data-role", "neutral");
    // The native `title` is gone: it could not be moused into or selected. The
    // same sentence now lives in the hover card, which can be.
    await expect(chip).not.toHaveAttribute("title", /./);
    await chip.hover();
    await expect(page.getByTestId("annotation-hovercard")).toContainText("stale since");

    await page.screenshot({
      path: `${SHOTS}/${testInfo.project.name}-c-stale-greyed.png`,
      fullPage: true,
    });
    await assertNoSeriousAxeViolations(page, '[data-testid="annotation-chip"]');
  });

  test("(d) a campaign-scoped gate and a fenced annotation both stay visible", async ({
    page,
  }, testInfo) => {
    await stubBackend(page, {
      annotations: payload([
        annotation({
          kind: "gate.pending",
          label: "gate pending",
          semantic_role: "warning",
          priority: 99,
          subject: {
            type: "campaign",
            campaign: "aegix-mobile-phase0-renewal",
          },
          details: {
            dependencies: "human-gate p0-09b-pr17-merge-approval",
            since: hoursAgo(6),
          },
        }),
        annotation({
          kind: "route-breaker.tripped",
          label: "route breaker",
          semantic_role: "danger",
          priority: 90,
          subject: { type: "campaign", campaign: "aegix-mobile-phase0-renewal" },
          details: { dependencies: "route-domain breaker", since: hoursAgo(30) },
        }),
        annotation({
          kind: "work-state.display",
          label: "orphaned run",
          semantic_role: "neutral",
          priority: 40,
          subject: { type: "terminal", terminal_id: "gone-0001", generation: "g-old" },
          details: { task: "canary-root-codex-reviewer-r1", lifecycle: "assigned" },
        }),
        // Written for a generation this terminal no longer has: dropped by the
        // fence, and the drop is stated rather than silent.
        annotation({
          label: "SUPERSEDED-OBLIGATION",
          subject: {
            type: "terminal",
            terminal_id: "t-native",
            generation: "a-previous-generation",
          },
        }),
      ]),
    });
    await page.goto("/");

    const panel = page.getByTestId("campaign-annotations");
    await expect(panel).toBeVisible();
    await expect(panel).toContainText("campaign aegix-mobile-phase0-renewal");
    await expect(panel).toContainText("human-gate p0-09b-pr17-merge-approval");
    await expect(panel.getByTestId("campaign-annotation-row")).toHaveCount(3);
    // The fence drop is reported, and the annotation itself is gone.
    await expect(page.getByTestId("annotation-fenced")).toContainText("1 annotation");
    await expect(page.getByText("SUPERSEDED-OBLIGATION")).toHaveCount(0);

    await page.screenshot({
      path: `${SHOTS}/${testInfo.project.name}-d-campaign-gate.png`,
      fullPage: true,
    });

    await assertTargetSize(page, '[data-testid="campaign-annotations"]');
    await assertNoSeriousAxeViolations(page, '[data-testid="campaign-annotations"]');
  });

  test("(e) the no-annotations control renders exactly as today", async ({
    page,
  }, testInfo) => {
    // The stub's default is the §9.5 "no conductor installed" answer, which is
    // what every other e2e spec in this suite already runs against.
    await stubBackend(page);
    await page.goto("/");

    await expect(page.getByText("cao-fleet")).toBeVisible();
    await expect(page.getByTestId("annotation-chip")).toHaveCount(0);
    await expect(page.getByTestId("campaign-annotations")).toHaveCount(0);
    // The fork's own surface is untouched: the status badge is still the row's
    // only statement about this worker.
    await expect(
      page.locator("#session-cao-fleet-terminals").getByText("Managed Live"),
    ).toBeVisible();

    await page.screenshot({
      path: `${SHOTS}/${testInfo.project.name}-e-no-annotations-control.png`,
      fullPage: true,
    });
  });

  test("an oversized fleet truncates visibly at both ends", async ({ page }, testInfo) => {
    const many = Array.from({ length: 6 }, (_, i) =>
      annotation({
        label: `chip-${i}`,
        semantic_role: ["info", "success", "warning", "accent", "danger", "neutral"][i],
        priority: 90 - i,
        details: { task: `t-${i}` },
      }),
    );
    await stubBackend(page, {
      annotations: payload(many, { coverage: "truncated", items_omitted: 42 }),
    });
    await page.goto("/");

    await expect(page.getByTestId("annotation-chip")).toHaveCount(3);
    await expect(page.getByTestId("annotation-overflow")).toHaveText("+3 more");
    await expect(page.getByTestId("annotation-omitted")).toContainText("42");
    // The marker is only useful if it is ON SCREEN. It used to be clipped off
    // the right edge of the card at 390 while `toHaveText` passed against the
    // DOM — vertical position is not the question, so scroll to it first.
    await page.getByTestId("annotation-overflow").scrollIntoViewIfNeeded();
    await expect(page.getByTestId("annotation-overflow")).toBeInViewport();
    await assertNoHorizontalClipping(page);

    await page.screenshot({
      path: `${SHOTS}/${testInfo.project.name}-f-truncation-markers.png`,
      fullPage: true,
    });
    await assertNoSeriousAxeViolations(
      page,
      '[data-testid="annotation-chip"]',
      '[data-testid="campaign-annotations"]',
    );
  });

  test("a 64-character label ellipsises instead of reflowing the row", async ({ page }) => {
    // 64 is exactly the server's MAX_LABEL, so this is an in-contract payload.
    await stubBackend(page, {
      annotations: payload([annotation({ label: "w".repeat(64) })]),
    });
    await page.goto("/");
    const chip = page.getByTestId("annotation-chip").first();
    await expect(chip).toBeVisible();

    // POLLED, not sampled once. `toBeVisible()` and `boundingBox()` are two
    // round trips, and the dashboard's own poll can replace the node between
    // them — `boundingBox()` then answers `null` and the non-null assertion
    // throws a TypeError that reads like a layout failure. Observed once in
    // five full-suite runs, only while the machine was saturated, and the same
    // shape exists on `main`, so this is the measurement being fragile rather
    // than the layout. Polling the measurement itself asserts the same bound
    // without the race.
    await expect
      .poll(async () => (await chip.boundingBox())?.height ?? null, {
        message: "chip wrapped into a multi-line blob",
      })
      .toBeLessThan(40);
    await assertNoHorizontalClipping(page);
    // Scoped to the identity row: the agent-type group heading carries the same
    // text, and it is the ROW's copy that used to vanish.
    const identity = chip.locator("xpath=../..");
    await expect(identity.getByText("spec-writer-k3")).toBeVisible();
  });

  test("the staleness dot keeps its geometry at every viewport", async ({ page }) => {
    // The dot is one of the two deliberate NON-COLOUR channels for staleness
    // (hollow vs filled). Without `shrink-0` flexbox squashed it to a 1.2px
    // sliver at 390 — the viewport with no hover at all, so neither the old
    // `title` nor the hover card that replaced it is reachable there.
    await stubBackend(page, {
      annotations: payload([
        annotation({ label: "waiting", priority: 90 }),
        annotation({ label: "review", priority: 80, semantic_role: "info" }),
        annotation({ label: "expired", priority: 70, valid_until: LONG_PAST }),
      ]),
    });
    await page.goto("/");
    await expect(page.getByTestId("annotation-chip")).toHaveCount(3);

    const dots = await page.evaluate(() =>
      Array.from(document.querySelectorAll('[data-testid="annotation-chip"] > span:first-child')).map(
        (el) => {
          const r = el.getBoundingClientRect();
          return { w: r.width, h: r.height };
        },
      ),
    );
    expect(dots).toHaveLength(3);
    for (const dot of dots) {
      expect(dot.w, "staleness dot width").toBeGreaterThanOrEqual(5);
      expect(dot.h, "staleness dot height").toBeGreaterThanOrEqual(5);
    }
  });

  test("the campaign surface cannot bury the fleet", async ({ page }) => {
    // Uncapped, 60 unplaced annotations measured 2936px tall and pushed the
    // Active Sessions header to y=3272 (desktop) / y=3544 (mobile) — four
    // screens of gate rows before a single worker. The server's own cap is 500.
    const activeSessionsTop = () =>
      page.evaluate(() => {
        const heading = Array.from(document.querySelectorAll("h3")).find(
          (h) => h.textContent?.trim() === "Active Sessions",
        )!;
        return heading.getBoundingClientRect().top + window.scrollY;
      });

    // The control: how far down the fleet already sits on this viewport,
    // before any annotation exists. The dashboard's own stat cards stack on a
    // phone, so the honest question is what the PANEL adds, not the absolute
    // offset.
    await stubBackend(page);
    await page.goto("/");
    await expect(page.getByText("cao-fleet")).toBeVisible();
    const control = await activeSessionsTop();

    const many = Array.from({ length: 100 }, (_, i) =>
      annotation({
        label: `gate-${i}`,
        priority: 99 - (i % 90),
        subject: { type: "campaign", campaign: `campaign-${i}` },
        details: { dependencies: `gate p0-${i}` },
      }),
    );
    await page.unrouteAll({ behavior: "ignoreErrors" });
    await stubBackend(page, { annotations: payload(many) });
    await page.reload();

    const panel = page.getByTestId("campaign-annotations");
    await expect(panel).toBeVisible();
    await expect(panel.getByTestId("campaign-annotation-row")).toHaveCount(8);
    await expect(panel.getByTestId("campaign-annotation-overflow")).toContainText("+92");

    const viewportH = page.viewportSize()!.height;
    const panelHeight = await panel.evaluate((el) => el.getBoundingClientRect().height);
    expect(panelHeight, "panel must fit inside one viewport").toBeLessThan(viewportH);
    expect(
      (await activeSessionsTop()) - control,
      "100 unplaced annotations must not push the fleet more than one screen down",
    ).toBeLessThan(viewportH);
  });

  test("chip contrast holds at both viewports", async ({ page }) => {
    // Recorded rather than remembered: the dashed-outline decision exists
    // BECAUSE `opacity-60` put the label under the floor, and a number nobody
    // re-measures is a number somebody reverts.
    //
    // ITERATES WHATEVER IS ON THE PAGE, not a fixed list of six roles. The
    // payload below adds a second, independently-coloured chip family drawn
    // with INLINE styles rather than token classes, and a gate that walked a
    // hard-coded role list would have gone on measuring six chips and reporting
    // success while twelve new colours went unchecked.
    //
    // Every one of the twelve palette slots is forced onto the page: the keys
    // are chosen to land in distinct buckets, two per row across six rows,
    // because a row caps its identity group at two.
    const BUCKET_KEYS = [
      "key-40", "key-4", "key-7", "key-1", "key-9", "key-2",
      "key-5", "key-15", "key-19", "key-0", "key-6", "key-27",
    ];
    const rows = fleet(7);
    await stubBackend(page, {
      terminals: rows,
      annotations: payload([
        ...["success", "info", "accent", "warning", "danger", "neutral"].map((role, i) =>
          annotation({
            label: role,
            semantic_role: role,
            priority: 90 - i,
            subject: { type: "campaign", campaign: `c-${role}` },
          }),
        ),
        // TWO COLOURED IDENTITY CHIPS ON THE CAMPAIGN PANEL, deliberately.
        // The panel is the LIGHTER backdrop and therefore the binding one: a
        // tint deepened from 10% to 20% measures 4.50 on a row card — which
        // rounds to exactly the floor and passes — and 4.34 on the panel,
        // which does not. A gate that only ever drew identity chips on rows
        // would have certified that change.
        ...["key-40", "key-4"].map((key, i) =>
          identity({
            label: `panel-${i}`,
            colour_key: key,
            priority: 84 - i,
            subject: { type: "campaign", campaign: `c-panel-${i}` },
          }),
        ),
        // The uncoloured variant is a THIRD rendering with its own foreground
        // and its own outline, so it is measured alongside the twelve.
        identity({
          label: "campaign",
          colour_key: "",
          priority: 7,
          subject: onRow(rows[6].id),
        }),
        ...BUCKET_KEYS.map((key, i) =>
          identity({
            label: `id-${i}`,
            colour_key: key,
            priority: 9,
            subject: onRow(rows[Math.floor(i / 2)].id),
          }),
        ),
      ]),
    });
    await page.goto("/");
    // 6 role chips + 2 coloured identity fill the campaign panel exactly to
    // its 8-row cap; the 12 bucket chips take two slots on each of six rows;
    // the uncoloured one gets a seventh row to itself.
    await expect(page.getByTestId("annotation-chip")).toHaveCount(21);

    const measured = await page.evaluate(() => {
      const parse = (value: string): [number, number, number, number] => {
        const n = value.match(/[\d.]+/g)!.map(Number);
        return [n[0], n[1], n[2], n.length > 3 ? n[3] : 1];
      };
      const over = (
        top: [number, number, number, number],
        bottom: [number, number, number],
      ): [number, number, number] => [
        top[0] * top[3] + bottom[0] * (1 - top[3]),
        top[1] * top[3] + bottom[1] * (1 - top[3]),
        top[2] * top[3] + bottom[2] * (1 - top[3]),
      ];
      const lum = (rgb: [number, number, number]) => {
        const f = rgb.map((c) => {
          const s = c / 255;
          return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
        });
        return 0.2126 * f[0] + 0.7152 * f[1] + 0.0722 * f[2];
      };
      return Array.from(
        document.querySelectorAll<HTMLElement>('[data-testid="annotation-chip"]'),
      ).map((chip) => {
        const label = chip.querySelector("span:nth-child(2)") as HTMLElement;
        // Composite every translucent background from the page down to the chip.
        let backdrop: [number, number, number] = [15, 15, 20];
        const stack: HTMLElement[] = [];
        for (let el: HTMLElement | null = chip; el; el = el.parentElement) stack.unshift(el);
        for (const el of stack) {
          const bg = parse(getComputedStyle(el).backgroundColor);
          if (bg[3] > 0) backdrop = over(bg, backdrop);
        }
        const fg = parse(getComputedStyle(label).color);
        const text = over(fg, backdrop);
        const [a, b] = [lum(text), lum(backdrop)].sort((x, y) => y - x);
        return {
          // Whichever channel this chip family names itself by. An identity
          // chip deliberately publishes no `data-role`: it is not making a
          // severity claim, and echoing one would invite this very test to
          // read severity off it.
          role: chip.getAttribute("data-role") ?? `identity-${chip.getAttribute("data-colour")}`,
          ratio: Math.round(((a + 0.05) / (b + 0.05)) * 10) / 10,
        };
      });
    });

    console.log("CHIP CONTRAST", JSON.stringify(measured));
    expect(measured.length, "the gate must measure every chip on the page").toBe(21);
    // NON-VACUITY: all twelve palette slots really were drawn, so a hash change
    // that collapsed the palette could not quietly shrink what is checked.
    const buckets = new Set(
      measured.map((m) => m.role).filter((r) => r.startsWith("identity-") && r !== "identity-none"),
    );
    expect(buckets.size, "every palette slot must be on the page").toBe(12);
    for (const { role, ratio } of measured) {
      expect(ratio, `${role} chip label contrast (WCAG AA 4.5:1)`).toBeGreaterThanOrEqual(4.5);
    }
  });

  test("adds no new accessibility violation to the page it renders into", async ({
    page,
  }) => {
    // The honest whole-page claim. The control's serious/critical set is
    // whatever the pre-existing dashboard chrome already produces; rendering a
    // full annotation surface on top of it must not add a single entry. This
    // is the assertion a scoped scan cannot make, and a whole-page "must be
    // zero" would be a claim about somebody else's chrome.
    await stubBackend(page);
    await page.goto("/");
    await expect(page.getByText("cao-fleet")).toBeVisible();
    const control = await seriousViolationIds(page);

    await page.unrouteAll({ behavior: "ignoreErrors" });
    await stubBackend(page, {
      annotations: payload([
        annotation({ label: "waiting", details: { task: "t", parked_at: hoursAgo(58) } }),
        annotation({
          label: "gate pending",
          priority: 99,
          subject: { type: "campaign", campaign: "aegix-mobile-phase0-renewal" },
          details: { dependencies: "human-gate p0-09b-pr17-merge-approval" },
        }),
        annotation({ label: "stale", valid_until: LONG_PAST }),
      ]),
    });
    await page.reload();
    await expect(page.getByTestId("annotation-chip").first()).toBeVisible();
    await expect(page.getByTestId("campaign-annotations")).toBeVisible();

    expect(await seriousViolationIds(page)).toEqual(control);
  });

  test("the work-state popover is reachable, complete and accessible", async ({
    page,
  }, testInfo) => {
    // The chips are spans and stay out of the tab order, so this button is the
    // ONLY keyboard and touch path to the detail. It therefore has to carry
    // everything the pointer-only hover card shows, and has to survive the same
    // axe gate the chips do.
    await stubBackend(page, {
      annotations: payload([
        annotation({
          details: {
            task: "p0-09b-b0-b1-implementation-r1",
            round: "12",
            lifecycle: "assigned",
          },
        }),
      ]),
    });
    await page.goto("/");

    const info = page.getByTestId("workstate-info-button");
    await expect(info).toBeVisible();
    await expect(info).toHaveAttribute("aria-expanded", "false");

    await info.click();
    const details = page.getByTestId("workstate-details");
    await expect(details).toBeVisible();
    await expect(info).toHaveAttribute("aria-expanded", "true");

    // Verbose by contract: the facets, the identity, and the envelope the chip
    // has no room for.
    await expect(details).toContainText("p0-09b-b0-b1-implementation-r1");
    await expect(details).toContainText("t-native");
    await expect(page.getByTestId("workstate-copy")).toBeVisible();

    // The card must sit ENTIRELY within the viewport. A card that hangs off the
    // bottom puts its footer — and with it the copy button — out of reach, and
    // at 390×844 that is exactly what an unclamped placement did.
    const box = (await details.boundingBox())!;
    const vp = page.viewportSize()!;
    expect(
      { top: Math.round(box.y), bottom: Math.round(box.y + box.height), vh: vp.height },
      "popover must fit the viewport",
    ).toEqual({
      top: expect.any(Number),
      bottom: expect.any(Number),
      vh: vp.height,
    });
    expect(box.y, "popover top edge on screen").toBeGreaterThanOrEqual(0);
    expect(box.y + box.height, "popover bottom edge on screen").toBeLessThanOrEqual(vp.height);

    // Contrast is measured by compositing rather than delegated to axe, because
    // a fixed popover overlaps page content and axe will not certify what it
    // cannot composite. See `assertCompositedContrast`.
    await assertCompositedContrast(page, '[data-testid="workstate-details"]');
    // Everything axe CAN judge about the card — roles, names, structure,
    // focus order — still has to pass, so the rule set is filtered to the one
    // reason code that is a measurement limitation rather than a finding.
    await assertAxeExcept(page, ["elmPartiallyObscuring"], '[data-testid="workstate-details"]');
    await page.screenshot({
      path: `${SHOTS}/${testInfo.project.name}-g-workstate-popover.png`,
      fullPage: true,
    });

    await page.keyboard.press("Escape");
    await expect(details).toHaveCount(0);
    await expect(info).toHaveAttribute("aria-expanded", "false");
  });

  test("two agents in one lane share a colour and a third lane does not", async ({
    page,
  }, testInfo) => {
    // THE HEADLINE CLAIM OF THE WHOLE FEATURE: an operator can see, at a
    // glance, which agents are working on the same thing. It is asserted on
    // COMPUTED style, because that is what the operator's eye receives — a
    // matching `data-colour` would pass even if the two chips painted
    // differently.
    //
    // TWO PINNED KEYS CHOSEN TO LAND IN DIFFERENT BUCKETS, never a universal
    // claim that different keys differ: twelve colours across a real fleet
    // collide BY DESIGN, and a test asserting otherwise would be asserting
    // something the design explicitly does not promise.
    const rows = fleet(3);
    await stubBackend(page, {
      terminals: rows,
      annotations: payload([
        identity({ label: "pr04", colour_key: "lane-alpha", subject: onRow("t-native") }),
        identity({ label: "pr04", colour_key: "lane-alpha", subject: onRow("t-1") }),
        identity({ label: "cond-0241", colour_key: "lane-beta", subject: onRow("t-2") }),
      ]),
    });
    await page.goto("/");
    await expect(page.getByTestId("annotation-chip")).toHaveCount(3);

    const painted = await page.evaluate(() =>
      Array.from(
        document.querySelectorAll<HTMLElement>('[data-testid="annotation-chip"]'),
      ).map((chip) => ({
        label: (chip.querySelector("span:nth-child(2)") as HTMLElement).textContent,
        bg: getComputedStyle(chip).backgroundColor,
        fg: getComputedStyle(chip.querySelector("span:nth-child(2)") as HTMLElement).color,
      })),
    );

    // Matched BY LABEL, not by DOM order: the dashboard sorts and groups its
    // rows, so which row paints first is its business and asserting on it here
    // would make this test fail for a reason that has nothing to do with colour.
    const shared = painted.filter((p) => p.label === "pr04");
    const other = painted.filter((p) => p.label === "cond-0241");
    expect(shared, "two agents in the one lane").toHaveLength(2);
    expect(other, "one agent in a different lane").toHaveLength(1);
    const [a, b] = shared;
    const [c] = other;
    expect({ bg: a.bg, fg: a.fg }, "one lane, one colour").toEqual({ bg: b.bg, fg: b.fg });
    expect(c.bg, "a different lane, a different colour").not.toBe(a.bg);
    expect(c.fg, "a different lane, a different colour").not.toBe(a.fg);
    // The colour is a property of the TOKEN, not of the row it landed on: the
    // two matching chips are on different terminals with different ids and
    // different generations.
    expect(rows[0].id).not.toBe(rows[1].id);

    await page.screenshot({
      path: `${SHOTS}/${testInfo.project.name}-h-lane-colour-sharing.png`,
      fullPage: true,
    });
    await assertNoHorizontalClipping(page);
  });

  test("a lane chip, a VCS chip and three annotations still leave the agent-profile name a box", async ({
    page,
  }, testInfo) => {
    // THE REGRESSION THAT HAS HAPPENED TWICE. At 390 the identity row had 282px
    // for 459px of content: flexbox shrank the `truncate` profile name to
    // nothing and clipped the chips off the card anyway, so the row lost the
    // one thing saying which worker it is. This feature adds two more chips to
    // that row, which is the largest increase it has ever taken.
    const branch = "agent/payment-pr04-foundation-r3";
    await stubBackend(page, {
      annotations: payload([
        annotation({ label: "waiting", priority: 90, details: { task: "t", parked_at: hoursAgo(58) } }),
        annotation({ label: "review", priority: 89, semantic_role: "info", details: { task: "t" } }),
        annotation({ label: "blocked", priority: 88, semantic_role: "danger", details: { task: "t" } }),
        identity({
          label: "pr04",
          priority: 8,
          colour_key: "lane-alpha",
          details: { "assigned.lane_source": "registry", "assigned.lane_scope": "lane" },
        }),
        identity({
          kind: "vcs",
          label: branch,
          priority: 7,
          colour_key: "wt-alpha",
          details: { "observed.branch": branch, "launch.repo": "dnd-scheduler" },
        }),
      ]),
    });
    await page.goto("/");
    await expect(page.getByTestId("annotation-chip")).toHaveCount(5);

    const row = page.locator("#session-cao-fleet-terminals");
    const profile = row.getByText("spec-writer-k3").first();
    await expect(profile).toBeVisible();
    const nameBox = (await profile.boundingBox())!;
    expect(nameBox.width, "agent profile name has a visible box").toBeGreaterThan(20);

    // NOT MERELY VISIBLE — NOT SQUEEZED. The name carries `truncate`, so a
    // flexbox that shrank it renders an ellipsis and keeps a non-zero box; a
    // width check alone would pass on a name reduced to "spec-w…". Comparing
    // the scroll width to the client width is the direct question.
    const squeezed = await profile.evaluate((el) => ({
      client: el.clientWidth,
      scroll: el.scrollWidth,
    }));
    expect(
      squeezed.scroll,
      "the chips squeezed the agent-profile name into an ellipsis",
    ).toBeLessThanOrEqual(squeezed.client + 1);

    // Nothing hangs off the right edge of the card at either viewport.
    await assertNoHorizontalClipping(page);

    // EVERY CHIP IS A DIRECT CHILD OF THE GROUP. `shrink-0`,
    // `whitespace-nowrap` and `basis-full` are all statements about that one
    // flex container, so an interposed wrapper silently redirects all three at
    // whatever it wraps. Structural because it is a structural invariant: it
    // must hold at every viewport and for every payload, not only for the ones
    // dense enough to make the difference visible.
    const structure = await page.evaluate(() => {
      const group = document.querySelector('[data-testid="annotation-group"]')!;
      const chips = Array.from(
        document.querySelectorAll<HTMLElement>('[data-testid="annotation-chip"]'),
      ).filter((c) => group.contains(c));
      return {
        chips: chips.length,
        orphaned: chips.filter((c) => c.parentElement !== group).length,
        tallest: Math.max(...chips.map((c) => Math.round(c.getBoundingClientRect().height))),
      };
    });
    expect(structure.chips, "the row must draw a full group").toBe(5);
    expect(structure.orphaned, "a chip separated from the flex container it is laid out by").toBe(0);
    // A chip that wrapped internally becomes a multi-line blob; the 64-char
    // label test pins the same number for the severity chip.
    expect(structure.tallest, "a chip wrapped into a multi-line blob").toBeLessThan(40);

    const vp = page.viewportSize()!;
    if (vp.width < 640) {
      // Below `sm` the whole chip group takes its own line. Measured rather
      // than asserted from the class list: `basis-full` only does this while
      // every chip is a DIRECT child of the group, and wrapping the identity
      // chips in an inner span is exactly how the regression comes back.
      const groupBox = (await page.getByTestId("annotation-group").boundingBox())!;
      expect(
        groupBox.y,
        "the chip group must drop below the identity line, not squeeze it",
      ).toBeGreaterThan(nameBox.y);
      // ...and the full branch is readable there, even though the chip clamps it.
      await expect(page.getByTestId("annotation-group")).toContainText(branch);
    }

    // If anything was withheld, the marker saying so is on screen.
    const marker = page.getByTestId("annotation-identity-overflow");
    if (await marker.count()) {
      await marker.scrollIntoViewIfNeeded();
      await expect(marker).toBeInViewport();
    }

    await page.screenshot({
      path: `${SHOTS}/${testInfo.project.name}-i-identity-row-density.png`,
      fullPage: true,
    });
  });

  test("the identity chips add no serious or critical axe result", async ({ page }) => {
    // READS `scan.incomplete` AS WELL AS `scan.violations`, unmodified. That is
    // not belt-and-braces: `aria-label` on a role-less `<span>` is
    // `aria-prohibited-attr`, which axe files as INCOMPLETE at serious impact
    // because it cannot verify the AT behaviour. Dropping `role="note"` from an
    // identity chip would therefore pass a violations-only gate on 100% of the
    // chips, which is the exact hole this repo has already fallen into once.
    await stubBackend(page, {
      annotations: payload([
        identity({ label: "pr04", colour_key: "lane-alpha" }),
        identity({ kind: "vcs", label: "agent/payment", priority: 7, colour_key: "wt-alpha" }),
        identity({
          label: "campaign",
          colour_key: "",
          subject: { type: "campaign", campaign: "aegix-mobile-phase0-renewal" },
        }),
        identity({
          label: "stale-lane",
          colour_key: "lane-beta",
          valid_until: LONG_PAST,
          subject: { type: "campaign", campaign: "aegix-mobile-phase0-renewal" },
        }),
      ]),
    });
    await page.goto("/");
    await expect(page.getByTestId("annotation-chip")).toHaveCount(4);

    // The chips are still spans, so this must still measure nothing. A control
    // added here would change the AAA target-size claim silently.
    expect(
      await assertTargetSize(page, '[data-testid="annotation-chip"]'),
      "identity chips must stay non-interactive",
    ).toBe(0);
    await assertNoSeriousAxeViolations(
      page,
      '[data-testid="annotation-chip"]',
      '[data-testid="campaign-annotations"]',
    );

    // Every chip keeps the role that makes its accessible name legal, and
    // every chip still says its identity in TEXT — the redundancy the whole
    // colour-blindness argument rests on.
    const chips = page.getByTestId("annotation-chip");
    for (let i = 0; i < (await chips.count()); i += 1) {
      const chip = chips.nth(i);
      await expect(chip).toHaveAttribute("role", "note");
      const label = await chip.locator("span:nth-child(2)").textContent();
      expect(label?.trim().length, "a chip drawn only as a swatch").toBeGreaterThan(0);
      expect(await chip.getAttribute("aria-label")).toContain(label!.trim());
    }
  });

  test("the popover Worker section carries the provenance classes, labelled, with full ids", async ({
    page,
  }, testInfo) => {
    const sha = "0e63aac5f65421ff481a7186b3f2e8de5030fd52";
    const branch = "agent/payment-pr04-foundation";
    await stubBackend(page, {
      annotations: payload([
        annotation({ label: "waiting", priority: 70, details: { task: "pr04-foundation-impl" } }),
        identity({
          label: "pr04",
          priority: 8,
          colour_key: "lane-alpha",
          details: {
            "assigned.assigned_at": hoursAgo(30),
            "assigned.lane_source": "registry",
            "assigned.role": "implementer",
          },
        }),
        identity({
          kind: "vcs",
          label: branch,
          priority: 7,
          colour_key: "wt-alpha",
          details: {
            "observed.branch": branch,
            "observed.commit": sha,
            "launch.repo": "dnd-scheduler",
            "launch.base_branch": "master",
          },
        }),
      ]),
    });
    await page.goto("/");

    await page.getByTestId("workstate-info-button").click();
    const details = page.getByTestId("workstate-details");
    await expect(details).toBeVisible();

    // Three labelled groups, above the annotations, in the producer's order.
    const groups = details.getByTestId("workstate-worker-group");
    await expect(groups).toHaveCount(3);
    expect(
      await groups.evaluateAll((els) => els.map((e) => e.getAttribute("data-group"))),
    ).toEqual(["assigned", "observed", "launch"]);
    const worker = details.getByTestId("workstate-worker");
    const workerTop = (await worker.boundingBox())!.y;
    const firstAnnotationTop = (await details.getByTestId("workstate-annotation").first().boundingBox())!.y;
    expect(workerTop, "the dossier sits above the annotations").toBeLessThan(firstAnnotationTop);

    // FULL LENGTH, and selectable. The chip clamps; this surface promises the
    // complete value, which is the entire reason it exists.
    await expect(worker).toContainText(sha);
    await expect(worker).toContainText(branch);
    await expect(details).toContainText("t-native");

    // The card fits the viewport at BOTH sizes — at 390×844 an unclamped
    // placement put the footer, and with it the copy button, out of reach.
    const box = (await details.boundingBox())!;
    const vp = page.viewportSize()!;
    expect(box.y, "popover top edge on screen").toBeGreaterThanOrEqual(0);
    expect(box.y + box.height, "popover bottom edge on screen").toBeLessThanOrEqual(vp.height);

    // UNMODIFIED, both of them. A floating card overlaps page content by
    // construction, so axe cannot composite it; the contrast it declines to
    // judge is measured directly instead of the gate being relaxed.
    await assertCompositedContrast(page, '[data-testid="workstate-details"]');
    await assertAxeExcept(page, ["elmPartiallyObscuring"], '[data-testid="workstate-details"]');

    await page.screenshot({
      path: `${SHOTS}/${testInfo.project.name}-j-worker-provenance.png`,
      fullPage: true,
    });
  });

  test("the hover card contains its facet values instead of clipping them", async ({
    page,
  }) => {
    // The real shapes that broke it, copied off the live fleet: a 51-character
    // branch, a full 40-character sha, and a worktree basename just as long.
    // Until the VCS chip every facet value was short (`task: p0-09b-r1`,
    // `role: implementer`) and fit whatever width the grid track happened to
    // take, so nothing here was ever under pressure.
    const BRANCH = "review/cond0225-0230-native-attestation-spec-sol-r5";
    await stubBackend(page, {
      annotations: payload([
        identity({
          kind: "vcs",
          label: BRANCH,
          colour_key: "wt-alpha",
          details: {
            "observed.branch": BRANCH,
            "observed.commit_short": "bbc6a75",
            "observed.commit": "bbc6a75cd37586ff99026746d3dd14acf3f39880",
            "launch.worktree": "cond0225-0230-native-attestation-spec-review-sol-r5",
            "launch.repo": "cao-conductor",
          },
        }),
      ]),
    });
    await page.goto("/");

    const chip = page.getByTestId("annotation-chip").first();
    await expect(chip).toBeVisible();
    await chip.hover();
    const card = page.getByTestId("annotation-hovercard");
    await expect(card).toBeVisible();

    // THE ASSERTION THAT WAS MISSING, and every neighbouring one passed while
    // the bug was live. The card carries `overflow-hidden` so its
    // square-cornered header and footer stay inside its radius; the same rule
    // silently clips any content wider than the box. A `1fr` grid track cannot
    // shrink below its min-content width, so the value column pushed 425px of
    // content into a 352px card and every value lost its last 72px --
    // `observed.branch` drew as `review/cond0225-0230-native-atte` + `sol-r5`,
    // a branch that does not exist, with no visual sign of truncation.
    // Visibility, the card's own bounding box, and axe were all satisfied.
    const fit = await card.evaluate((el) => {
      const r = el.getBoundingClientRect();
      let worst = 0;
      for (const kid of el.querySelectorAll("dd,dt,span,p")) {
        const kr = kid.getBoundingClientRect();
        if (kr.width) worst = Math.max(worst, kr.right - r.right);
      }
      return { scrollW: el.scrollWidth, clientW: el.clientWidth, worstSpill: worst };
    });
    expect(
      fit.scrollW,
      "hover card content is wider than the card that clips it",
    ).toBeLessThanOrEqual(fit.clientW + 1);
    expect(
      fit.worstSpill,
      "a facet row extends past the card's right edge",
    ).toBeLessThanOrEqual(1);

    // Inside the box is necessary and not sufficient: the value must also be
    // all there. Asserted on the `dd` with the WHOLE string, because a clipped
    // or re-joined value still contains the prefix a `hasText` check accepts.
    const values = card.locator("dd");
    await expect(values.filter({ hasText: "review/cond0225" })).toHaveText([BRANCH]);
    await expect(values.filter({ hasText: "bbc6a75cd" })).toHaveText([
      "bbc6a75cd37586ff99026746d3dd14acf3f39880",
    ]);
  });

  test("a body the route should never send degrades to the control rendering", async ({
    page,
  }) => {
    await stubBackend(page, {
      // Deliberately not an AnnotationsResponse.
      annotations: { annotations: "not a list" } as unknown as AnnotationsResponse,
    });
    await page.goto("/");

    await expect(page.getByText("cao-fleet")).toBeVisible();
    await expect(page.getByTestId("annotation-chip")).toHaveCount(0);
    await expect(page.getByTestId("campaign-annotations")).toHaveCount(0);
  });
});
