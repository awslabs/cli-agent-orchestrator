import { test, expect, Page } from "@playwright/test";
import { AxeBuilder } from "@axe-core/playwright";
import type { Annotation } from "../src/api";
import { stubBackend, stubTerminals, T_NATIVE_GENERATION, type StubCommunicationsList } from "./stub";

// Communications catalog modal (catalog design §8/§9/§10), run at 1280×800
// and 390×844 against the stubbed server. The security test at the bottom is
// the load-bearing one: a page load whose captured content is an attack
// payload must produce no dialog, no console error, and no request to the
// attacker URL — asserted with page-level listeners, not by reading the DOM
// policy helpers.

const SHOTS = "e2e/__screenshots__/communications";

const TASK = "task-occ-e2e-1";

function doc(attachmentId: string, overrides: Record<string, unknown> = {}) {
  return {
    attachment_id: attachmentId,
    document_id: `doc-${attachmentId}`,
    role: "body",
    display_name: "report.md",
    media_type: "text/markdown",
    sha256: "a".repeat(64),
    byte_size: 42,
    blob_id: "a".repeat(64),
    content_state: "present",
    capture_kind: "inline-message",
    redaction_applied: false,
    ...overrides,
  };
}

function comm(id: string, recordedAt: string, overrides: Record<string, unknown> = {}) {
  return {
    communication_id: id,
    project_id: "project",
    session_id: "session",
    lane_id: "lane",
    task_occurrence_id: TASK,
    goal_version: "1",
    kind: "report",
    report_scope: null,
    authored_by_type: "agent",
    authored_by_id: "agent-1",
    authored_at: recordedAt,
    recorded_at: recordedAt,
    title: null,
    delivery_state: "delivered",
    visibility: "internal",
    request_key: null,
    supersedes_communication_id: null,
    superseded_by: null,
    body: null,
    documents: [],
    ...overrides,
  };
}

function catalogList(items: unknown[], overrides: Record<string, unknown> = {}): StubCommunicationsList {
  return {
    schema: "cao-communications-index-v1",
    coverage: "complete",
    reasons: [],
    communications: items,
    next_cursor: null,
    total: items.length,
    ...overrides,
  };
}

const FINAL = comm("c-final", "2026-08-18T02:00:00Z", {
  title: "Final report",
  report_scope: "final",
  body: doc("att-body-final", { display_name: "final-report.md" }),
  documents: [
    doc("att-notes", { role: "attachment", display_name: "notes.md", byte_size: 1234 }),
  ],
});
const ASSIGNMENT = comm("c-assign", "2026-08-18T01:00:00Z", {
  kind: "assignment",
  title: "Assignment",
  body: doc("att-body-assign", { media_type: "text/plain", display_name: "assignment.txt" }),
});

const FINAL_CONTENT = [
  "# Final report",
  "",
  "The change is **complete** and verified:",
  "",
  "- [x] tests green",
  "- [x] review addressed",
  "",
  "| check | result |",
  "| - | - |",
  "| unit | pass |",
].join("\n");

function catalogAnnotations(): Annotation[] {
  return [
    {
      namespace: "cao-conductor",
      kind: "work-state.display",
      version: 1,
      label: "active",
      semantic_role: "info",
      priority: 60,
      subject: {
        type: "terminal",
        terminal_id: stubTerminals[0].id,
        generation: T_NATIVE_GENERATION,
        task_occurrence_id: TASK,
      },
      valid_until: "2999-01-01T00:00:00Z",
      details: { communication_count: "2", latest_communication_kind: "report" },
      source: "project",
    },
  ];
}

function catalogOptions() {
  return {
    annotations: {
      annotation_schema: "cao-annotations-v1",
      coverage: "complete",
      sources_read: 1,
      sources_failed: 0,
      items_dropped: 0,
      items_omitted: 0,
      reasons: [],
      annotations: catalogAnnotations(),
    },
    communications: {
      list: catalogList([FINAL, ASSIGNMENT]),
      details: {
        "c-final": { communication: FINAL, content: FINAL_CONTENT, reason: null },
        "c-assign": { communication: ASSIGNMENT, content: "Build the thing.\nExactly as written.", reason: null },
      },
      attachments: {
        "att-notes": { document: FINAL.documents[0], content: "# Notes\n\nattachment body", reason: null },
      },
    },
  };
}

async function openFleet(page: Page) {
  await page.goto("/");
  await page.getByText("cao-fleet").waitFor();
}

test.describe("communications catalog modal", () => {
  test("chip and row control open the catalog; selection deep-links", async ({ page }, testInfo) => {
    const isMobile = testInfo.project.name === "mobile-chromium";
    await stubBackend(page, catalogOptions());
    await openFleet(page);

    // The chip is a real button, and the row control carries the count facet.
    const chip = page.getByTestId("annotation-chip").first();
    await expect(chip).toHaveAttribute("data-actionable", "true");
    const entry = page.getByTestId("communications-button");
    await expect(entry).toBeVisible();
    await expect(entry).toContainText("2");

    await entry.click();
    const modal = page.getByTestId("communications-modal");
    await expect(modal).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`task_occurrence_id=${TASK}`));

    // Desktop shows the latest record implicitly; on mobile the list comes
    // first and a tap enters the reader.
    if (isMobile) await modal.getByTestId("communication-item").filter({ hasText: "Final report" }).click();
    await expect(modal.getByTestId("md-rendered").getByRole("heading", { name: "Final report" })).toBeVisible();
    await expect(modal.getByTestId("scope-badge").first()).toHaveText("final report");
    await expect(modal.getByTestId("scope-disclaimer")).toBeVisible();

    // Selecting the assignment deep-links the selection.
    if (isMobile) await modal.getByTestId("reader-back").click();
    await modal.getByTestId("communication-item").filter({ hasText: "Assignment" }).click();
    await expect(page).toHaveURL(/communication_id=c-assign/);
    await expect(modal.getByTestId("content-raw")).toHaveText("Build the thing.\nExactly as written.");

    // Raw/rendered toggle on the Markdown final report.
    if (isMobile) await modal.getByTestId("reader-back").click();
    await modal.getByTestId("communication-item").filter({ hasText: "Final report" }).click();
    const rendered = modal.getByTestId("md-rendered");
    await expect(rendered.getByRole("checkbox").first()).toBeDisabled();
    await expect(rendered.getByRole("table")).toBeVisible();
    await modal.getByRole("button", { name: "Raw" }).click();
    await expect(modal.getByTestId("content-raw")).toContainText("# Final report");
    await modal.getByRole("button", { name: "Rendered" }).click();

    // Attachments open inline with their own controls.
    await modal.getByTestId("attachment-open").click();
    await expect(modal.getByTestId("attachment-row").getByTestId("md-rendered")).toContainText("attachment body");

    await page.screenshot({ path: `${SHOTS}/${testInfo.project.name}-modal.png` });

    // Escape closes and strips the deep link.
    await page.keyboard.press("Escape");
    await expect(modal).not.toBeVisible();
    await expect(page).not.toHaveURL(/task_occurrence_id/);
  });

  test("mobile: full-height list, then a back-navigable reader", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile-chromium", "mobile-only layout assertions");
    await stubBackend(page, catalogOptions());
    await openFleet(page);

    await page.getByTestId("communications-button").click();
    const modal = page.getByTestId("communications-modal");
    await expect(modal.getByTestId("communication-item").first()).toBeVisible();

    // Tapping a row opens the full-height reader; the header back button
    // returns to the list and clears the URL selection.
    await modal.getByTestId("communication-item").filter({ hasText: "Assignment" }).click();
    const back = modal.getByTestId("reader-back");
    await expect(back).toBeVisible();
    await expect(modal.getByTestId("content-raw")).toBeVisible();
    await page.screenshot({ path: `${SHOTS}/${testInfo.project.name}-reader.png` });
    await back.click();
    await expect(page).not.toHaveURL(/communication_id/);
    await expect(modal.getByTestId("communication-item").first()).toBeVisible();
  });

  test("a deep link opens the record; an unknown id is the stable not-found state", async ({ page }) => {
    await stubBackend(page, catalogOptions());
    await page.goto(`/?task_occurrence_id=${TASK}&communication_id=c-final`);
    const modal = page.getByTestId("communications-modal");
    await expect(modal).toBeVisible();
    await expect(modal.getByTestId("md-rendered")).toContainText("The change is complete");

    await page.goto(`/?task_occurrence_id=${TASK}&communication_id=c-unknown`);
    await expect(modal.getByTestId("detail-not-found")).toBeVisible();
    await expect(modal.getByTestId("detail-not-found")).toContainText("not in the catalog");
    await modal.getByTestId("communications-close").click();
    await expect(modal).not.toBeVisible();
  });

  test("no catalog installed: byte-quiet dashboard, inert chips", async ({ page }) => {
    await stubBackend(page, {
      annotations: {
        annotation_schema: "cao-annotations-v1",
        coverage: "complete",
        sources_read: 1,
        sources_failed: 0,
        items_dropped: 0,
        items_omitted: 0,
        reasons: [],
        annotations: catalogAnnotations(),
      },
      // No `communications` option: the list route answers unavailable+missing.
    });
    await openFleet(page);
    const chip = page.getByTestId("annotation-chip").first();
    await expect(chip).toBeVisible();
    await expect(chip).not.toHaveAttribute("data-actionable", "true");
    await expect(page.getByTestId("communications-button")).toHaveCount(0);
    // The rest of the dashboard is untouched.
    await expect(page.getByRole("button", { name: "Terminal" })).toBeVisible();
  });

  test("the open modal passes axe with no serious or critical violations", async ({ page }, testInfo) => {
    await stubBackend(page, catalogOptions());
    await openFleet(page);
    await page.getByTestId("communications-button").click();
    const modal = page.getByTestId("communications-modal");
    // Mobile opens on the list; one tap enters the reader.
    if (testInfo.project.name === "mobile-chromium") {
      await modal.getByTestId("communication-item").first().click();
    }
    await expect(page.getByTestId("md-rendered")).toBeVisible();
    const results = await new AxeBuilder({ page })
      .include('[data-testid="communications-modal"]')
      .analyze();
    const violations = results.violations.filter(v => v.impact === "serious" || v.impact === "critical");
    expect(violations).toEqual([]);
  });

  test("an attack payload executes and fetches nothing on a real page load", async ({ page }, testInfo) => {
    const ATTACK = [
      "# Report",
      "",
      "<script>alert('xss-script')</script>",
      "",
      "<img src=x onerror=\"alert('xss-img')\">",
      "",
      "[click](javascript:alert('xss-link'))",
      "",
      "[pixel data](data:text/html,<script>alert('xss-data')</script>)",
      "",
      "![tracker](https://evil.example/pixel.png)",
      "",
      "[proto](//evil.example/protocol-relative)",
    ].join("\n");
    const attacked = comm("c-attack", "2026-08-18T03:00:00Z", {
      title: "Attack report",
      body: doc("att-body-attack"),
    });

    const dialogs: string[] = [];
    page.on("dialog", d => {
      dialogs.push(d.message());
      void d.dismiss();
    });
    const requests: string[] = [];
    page.on("request", r => requests.push(r.url()));
    const consoleErrors: string[] = [];
    page.on("console", m => {
      if (m.type() === "error") consoleErrors.push(m.text());
    });
    page.on("pageerror", e => consoleErrors.push(String(e)));

    await stubBackend(page, {
      ...catalogOptions(),
      communications: {
        list: catalogList([attacked]),
        details: { "c-attack": { communication: attacked, content: ATTACK, reason: null } },
        attachments: {},
      },
    });
    await openFleet(page);
    await page.getByTestId("communications-button").click();
    const modal = page.getByTestId("communications-modal");
    // Mobile opens on the list; one tap enters the reader.
    if (testInfo.project.name === "mobile-chromium") {
      await modal.getByTestId("communication-item").first().click();
    }
    await expect(modal.getByTestId("md-rendered")).toBeVisible();

    // Nothing executed, nothing fetched, nothing even carries the URLs.
    expect(dialogs).toEqual([]);
    expect(requests.filter(u => u.includes("evil.example"))).toEqual([]);
    expect(consoleErrors).toEqual([]);
    await expect(modal.locator("script")).toHaveCount(0);
    await expect(modal.locator("img")).toHaveCount(0);
    await expect(modal.locator('[src*="evil.example"]')).toHaveCount(0);
    await expect(modal.locator('a[href^="javascript:"]')).toHaveCount(0);
    await expect(modal.locator('a[href^="data:"]')).toHaveCount(0);
    await expect(modal.locator('a[href^="//"]')).toHaveCount(0);
    // The image placeholder shows the alt text without fetching.
    await expect(modal.getByTestId("md-image-placeholder")).toContainText("tracker");
    await page.screenshot({ path: `${SHOTS}/attack-payload-inert.png` });
  });
});
