import { test, expect, Page } from "@playwright/test";
import { AxeBuilder } from "@axe-core/playwright";
import type { Annotation, AnnotationsResponse } from "../src/api";
import { stubBackend, T_NATIVE_GENERATION } from "./stub";

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
  const serious = (results: typeof scan.violations) =>
    results
      .filter((v) => v.impact === "serious" || v.impact === "critical")
      .flatMap((v) => v.nodes.map((n) => `${v.id}: ${n.target.join(" ")}`));
  expect(serious(scan.violations), "axe violations").toEqual([]);
  expect(serious(scan.incomplete), "axe incomplete").toEqual([]);
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
    await expect(chip).toHaveAttribute("title", /stale since/);

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

    const box = (await chip.boundingBox())!;
    expect(box.height, "chip wrapped into a multi-line blob").toBeLessThan(40);
    await assertNoHorizontalClipping(page);
    // Scoped to the identity row: the agent-type group heading carries the same
    // text, and it is the ROW's copy that used to vanish.
    const identity = chip.locator("xpath=../..");
    await expect(identity.getByText("spec-writer-k3")).toBeVisible();
  });

  test("the staleness dot keeps its geometry at every viewport", async ({ page }) => {
    // The dot is one of the two deliberate NON-COLOUR channels for staleness
    // (hollow vs filled). Without `shrink-0` flexbox squashed it to a 1.2px
    // sliver at 390, where the `title` fallback does not exist either.
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
    await stubBackend(page, {
      annotations: payload(
        ["success", "info", "accent", "warning", "danger", "neutral"].map((role, i) =>
          annotation({
            label: role,
            semantic_role: role,
            priority: 90 - i,
            subject: { type: "campaign", campaign: `c-${role}` },
          }),
        ),
      ),
    });
    await page.goto("/");
    await expect(page.getByTestId("annotation-chip")).toHaveCount(6);

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
          role: chip.getAttribute("data-role"),
          ratio: Math.round(((a + 0.05) / (b + 0.05)) * 10) / 10,
        };
      });
    });

    console.log("CHIP CONTRAST", JSON.stringify(measured));
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
