// @vitest-environment jsdom
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Root } from "../Root.tsx";
import { chooseToken } from "../token.ts";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const noStorage = { getItem: () => null };

describe("Root", () => {
  it("a bad ?server= shows why and connects to nothing", () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    render(<Root choice={chooseToken("?server=nonsense", noStorage, () => true)} threadId="t" />);
    expect(screen.getByRole("alert").textContent).toBe(
      "Not connecting to nonsense: the server URL is not valid: it must be an http or https URL.",
    );
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("a non-http(s) ?server= is refused the same way, with no confirm", () => {
    const confirm = vi.fn(() => true);
    render(<Root choice={chooseToken("?server=javascript:alert(1)", { getItem: () => "tok" }, confirm)} threadId="t" />);
    expect(screen.getByRole("alert").textContent).toContain("Not connecting to javascript:alert(1)");
    expect(confirm).not.toHaveBeenCalled();
  });

  it("a good server renders the dashboard; a missing EventSource shows as refused, not a blank page", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response('data: {"type":"RUN_FINISHED","outcome":{"type":"success"}}\n\n')));
    await act(async () => {
      render(<Root choice={{ ok: true, server: "http://127.0.0.1:9889", note: "token not sent" }} threadId="t" />);
    });
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("CAO operator dashboard");
    expect(document.body.textContent).toContain("Ambient stream: refused (EventSource is not available");
    expect(document.body.textContent).toContain("; token not sent");
  });

  it("P3-2: when the ambient stream opens again after a drop, interrupts are listed again", async () => {
    const runCalls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        runCalls.push(new URL(url).pathname);
        return new Response('data: {"type":"RUN_FINISHED","outcome":{"type":"success"}}\n\n');
      }),
    );
    let source: { onopen: ((e: unknown) => void) | null; onerror: ((e: unknown) => void) | null; readyState: number } | undefined;
    vi.stubGlobal(
      "EventSource",
      class {
        readyState = 1;
        onopen: ((e: unknown) => void) | null = null;
        onmessage: ((e: { data: string }) => void) | null = null;
        onerror: ((e: unknown) => void) | null = null;
        constructor() {
          source = this;
        }
        close() {}
      },
    );
    await act(async () => {
      render(<Root choice={{ ok: true, server: "http://127.0.0.1:9889", note: "" }} threadId="t" />);
    });
    await act(async () => {
      source!.onopen!({});
    });
    const atMount = runCalls.filter((p) => p === "/agui/v1/run").length;
    expect(atMount).toBe(1);
    await act(async () => {
      source!.readyState = 0;
      source!.onerror!({});
      source!.onopen!({});
    });
    expect(runCalls.filter((p) => p === "/agui/v1/run").length).toBe(atMount + 1);
  });

  it("#1157: a reopen marks the timeline's open calls unconfirmed", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response('data: {"type":"RUN_FINISHED","outcome":{"type":"success"}}\n\n')));
    let source: { onopen: ((e: unknown) => void) | null; onmessage: ((e: { data: string }) => void) | null; onerror: ((e: unknown) => void) | null; readyState: number } | undefined;
    vi.stubGlobal(
      "EventSource",
      class {
        readyState = 1;
        onopen: ((e: unknown) => void) | null = null;
        onmessage: ((e: { data: string }) => void) | null = null;
        onerror: ((e: unknown) => void) | null = null;
        constructor() {
          source = this;
        }
        close() {}
      },
    );
    await act(async () => {
      render(<Root choice={{ ok: true, server: "http://127.0.0.1:9889", note: "" }} threadId="t" />);
    });
    await act(async () => {
      source!.onopen!({});
      source!.onmessage!({ data: JSON.stringify({ type: "TOOL_CALL_START", toolCallId: "a", metadata: { cao: { orchestrationType: "assign" } } }) });
    });
    expect(screen.getByTestId("call-state").textContent).toBe("open");
    await act(async () => {
      source!.readyState = 0;
      source!.onerror!({});
      source!.onopen!({});
    });
    expect(screen.getByTestId("call-state").textContent).toBe("open; its end was not seen across a reconnect");
  });
});
