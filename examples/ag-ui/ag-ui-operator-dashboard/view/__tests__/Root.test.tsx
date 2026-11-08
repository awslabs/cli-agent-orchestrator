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
});
