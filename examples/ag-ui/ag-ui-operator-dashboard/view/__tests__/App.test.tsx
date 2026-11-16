// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { App } from "../App.tsx";
import { createStore } from "../store.ts";
import { fakeFetch, fixtureFrames, fixtureJson, fixtureText, interruptsOf, sse } from "../../core/__tests__/helpers.ts";
import type { Frame } from "../../core/index.ts";

afterEach(cleanup);

const BASE = "http://127.0.0.1:9894";
const edit = interruptsOf("edit-offered.generated.sse")[0];

function mount(replies: Parameters<typeof fakeFetch>[0]) {
  const { fetch, calls } = fakeFetch(replies);
  const store = createStore({ base: BASE, threadId: "view", fetchImpl: fetch });
  render(<App store={store} server={BASE} ambientStatus="open" />);
  return { store, calls };
}

async function click(name: string | RegExp, scope: HTMLElement = document.body) {
  await act(async () => {
    fireEvent.click(within(scope).getByRole("button", { name }));
  });
}

describe("App", () => {
  it("no Edit field when responseSchema has no editedArgs", async () => {
    mount([{ sse: fixtureText("one-interrupt.sse") }]);
    await click("Check for approvals");
    expect(screen.getAllByRole("article")).toHaveLength(1);
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.getByTestId("state").textContent).toBe("pending");
  });

  it("blank Edit is refused on screen and nothing is sent", async () => {
    const listing = sse({ type: "RUN_FINISHED", outcome: { type: "interrupt", interrupts: [edit] } });
    const { calls } = mount([{ sse: listing }]);
    await click("Check for approvals");
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "   " } });
    await click("Send edited answer");
    expect(calls).toHaveLength(1);
    expect(screen.getByTestId("state").textContent).toBe("refused: edited text is empty; nothing was sent");
  });

  it("two approvals: says how many are still to decide, then sends one batch", async () => {
    const { calls } = mount([{ sse: fixtureText("two-interrupts.sse") }, { sse: fixtureText("resume-batched.sse") }]);
    await click("Check for approvals");
    const [first, second] = screen.getAllByRole("article");
    await click("Approve", first);
    expect(screen.getByRole("status").textContent).toMatch(/Still to decide: 1/);
    await click("Undo", first);
    await click("Approve", first);
    await click("Deny", second);
    expect(calls).toHaveLength(2);
    expect(screen.getAllByTestId("state").map((e) => e.textContent)).toEqual([
      "delivered (decision applied: not reported by the run plane)",
      "delivered (decision applied: not reported by the run plane)",
    ]);
  });

  it("shows a run failure verbatim", async () => {
    mount([{ status: 404, json: { detail: "AG-UI surface disabled" } }]);
    await click("Check for approvals");
    expect(screen.getByRole("alert").textContent).toBe("AG-UI is off (HTTP 404): AG-UI surface disabled");
  });

  it("renders all six components, the operator sender, and hostile props as text", async () => {
    const { store } = mount([]);
    await act(async () => {
      for (const f of fixtureFrames("ambient.sse").filter((f) => f.type === "CUSTOM").slice(0, 8)) {
        await store.controller.ambient(f);
      }
    });
    const panel = screen.getByRole("region", { name: "Generative UI" });
    for (const name of ["approval_card", "choice_prompt", "diff_summary", "progress", "metric", "agent_card"]) {
      expect(within(panel).getAllByRole("article", { name }).length).toBeGreaterThan(0);
    }
    expect(panel.textContent).toContain("sender operator (not a subagent)");
    expect(panel.textContent).toContain("<script>alert(1)</script>");
    expect(panel.querySelector("img, script")).toBeNull();
    expect(within(panel).queryAllByRole("button")).toHaveLength(0);
  });

  it("an off-list frame shows a refusal and never its props", async () => {
    const { store } = mount([]);
    // The captured RAW frame carries no props, so add some, as a hostile producer might.
    const raw = fixtureJson<Frame>("raw-offlist.json");
    const withProps: Frame = { ...raw, event: { ...(raw.event as object), props: { html: "<b>secret-raw</b>" } } };
    const custom: Frame = {
      type: "CUSTOM",
      name: "cao.generative_ui",
      metadata: { cao: { eventId: "offlist-custom" } },
      value: { component: "iframe_embed", props: { src: "secret-custom" } },
    };
    await act(async () => {
      await store.controller.ambient(withProps);
      await store.controller.ambient(custom);
    });
    const refused = screen.getAllByRole("article", { name: "refused component" });
    expect(refused.map((r) => r.textContent)).toEqual([
      expect.stringContaining('Refused: "iframe_embed.8688ac89" is not on the allow-list'),
      expect.stringContaining('Refused: "iframe_embed" is not on the allow-list'),
    ]);
    const panel = screen.getByRole("region", { name: "Generative UI" });
    expect(panel.textContent).not.toContain("secret-raw");
    expect(panel.textContent).not.toContain("secret-custom");
  });

  it("a refusal's fold count is shown as text", async () => {
    const { store } = mount([]);
    const raw = fixtureJson<Frame>("raw-offlist.json");
    const folded: Frame = { ...raw, event: { ...(raw.event as object), more_refused: 7 } };
    await act(async () => {
      await store.controller.ambient(folded);
    });
    const [refused] = screen.getAllByRole("article", { name: "refused component" });
    expect(refused.textContent).toContain("And 7 more refused.");
  });

  it("a failed reconcile offers a retry, and the retry clears it", async () => {
    const A = interruptsOf("one-interrupt.sse")[0];
    const { calls } = mount([
      { sse: sse({ type: "RUN_FINISHED", outcome: { type: "interrupt", interrupts: [A] } }) },
      { status: 403, json: { detail: "cao:write required when resume[] is non-empty" } },
      { throws: new TypeError("Failed to fetch") },
      { sse: sse({ type: "RUN_FINISHED", outcome: { type: "success" } }) },
    ]);
    await click("Check for approvals");
    await click("Approve");
    expect(screen.getByRole("alert").textContent).toContain("The check that followed also failed");
    await click("Retry the check");
    expect(calls).toHaveLength(4);
    expect(screen.queryByRole("button", { name: "Retry the check" })).toBeNull();
    expect(screen.getByTestId("state").textContent).toBe("delivered (unconfirmed)");
  });

  it("a ready batch waiting on an in-flight run is visible, then sent when the run ends", async () => {
    const A = interruptsOf("one-interrupt.sse")[0];
    const listing = sse({ type: "RUN_FINISHED", outcome: { type: "interrupt", interrupts: [A] } });
    const { fetch: base, calls } = fakeFetch([{ sse: listing }, { sse: fixtureText("resume-approve.sse") }]);
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    let n = 0;
    const gated = (async (input: string, init?: RequestInit) => {
      n += 1;
      if (n === 2) {
        await gate; // the second listing run stays in flight until released
        return new Response(listing, { status: 200 });
      }
      return base(input, init);
    }) as unknown as typeof fetch;
    const store = createStore({ base: BASE, threadId: "view", fetchImpl: gated });
    render(<App store={store} server={BASE} ambientStatus="open" />);
    await click("Check for approvals");
    let inFlight: Promise<unknown> = Promise.resolve();
    await act(async () => {
      inFlight = store.controller.refresh();
    });
    await click("Approve");
    expect(screen.getByRole("status").textContent).toMatch(/Every open approval is decided/);
    expect(screen.queryByRole("button", { name: /Send decisions/ })).toBeNull();
    expect(calls).toHaveLength(1); // nothing goes out while the run is in flight
    await act(async () => {
      release();
      await inFlight;
    });
    expect(calls).toHaveLength(2);
    expect((calls[1].body as { resume: unknown[] }).resume).toHaveLength(1);
    expect(screen.queryByText(/Every open approval is decided/)).toBeNull();
    expect(screen.getByTestId("state").textContent).toBe("delivered (decision applied: not reported by the run plane)");
  });
});
